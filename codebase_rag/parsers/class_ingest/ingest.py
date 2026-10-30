from __future__ import annotations

from collections.abc import Collection, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ...language_spec import LanguageSpec, module_directory_qn
from ...node_text import safe_decode_text, sorted_captures
from ...types_defs import (
    CppDefinitionSpan,
    DeferredInherit,
    FunctionLocation,
    PropertyDict,
)
from ...utils import qn_markers
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..anchor_hash import anchor_hash_props
from ..cpp import class_ingest as cpp_class_ingest
from ..cpp.utils import record_cpp_definition_span
from ..csharp import utils as csharp_utils
from ..csharp.arity import CSharpArityIndex
from ..dart import utils as dart_utils
from ..dart.type_inference import DartTypeInferenceEngine
from ..definition_facts import DefinitionFacts
from ..enum_variants import emit_declared_variants, is_python_enum
from ..field_nodes import PendingFieldType, emit_declared_fields
from ..go import GoTypeInferenceEngine
from ..go import interfaces as go_ifaces
from ..java import utils as java_utils
from ..js_ts import utils as js_ts_utils
from ..js_ts.utils import mark_js_ts_module_private
from ..method_ingest import MethodIngestHost, ingest_method
from ..parameter_nodes import csharp_call_shape
from ..php import namespaces as php_namespaces
from ..python import (
    external_stdlib_base_method_names,
    resolve_class_name,
    under_type_checking,
)
from ..python.class_aliases import scan_class_body_aliases
from ..rust import class_ingest as rust_class_ingest
from ..rust import utils as rs_utils
from ..utils import (
    decorated_start_props,
    extract_modifiers_and_decorators,
    function_span_key,
    module_qn_for_entity,
    written_simple_name,
)
from . import base_targets as bt
from . import cpp_modules
from . import definition_label as nt
from . import identity as id_
from . import method_override as mo
from . import parent_extraction as pe
from . import relationships as rel
from .utils import (
    body_method_nodes,
    csharp_has_override_modifier,
    find_child_by_type,
    skip_method,
)

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


def _class_start_point(
    class_node: Node, language: cs.SupportedLanguage
) -> tuple[int, int]:
    if language == cs.SupportedLanguage.CSHARP:
        # Skip a leading `#if [Attr] #endif` directive so the start line is
        # the conditional attribute, not the `#if` line (matches Roslyn).
        from ..csharp import utils as csharp_utils

        return csharp_utils.definition_start_point(class_node)
    return class_node.start_point[0] + 1, class_node.start_point[1]


def _csharp_record_parameter_types(
    member_node: Node, field_types: dict[str, str]
) -> None:
    # A record's positional parameters ARE public properties of
    # the record type; record them as members so receiver typing
    # and the delegate-invoke gate see them (`Callback();` on a
    # record param is Action.Invoke, not a first-party call).
    for pl in member_node.children:
        if pl.type != cs.TS_CSHARP_PARAMETER_LIST:
            continue
        for prm in pl.children:
            if prm.type != cs.TS_CSHARP_PARAMETER:
                continue
            pname = safe_decode_text(prm.child_by_field_name(cs.FIELD_NAME))
            ptype = safe_decode_text(prm.child_by_field_name(cs.FIELD_TYPE))
            if pname and ptype:
                field_types.setdefault(pname, csharp_utils.annotate_type_ref(ptype))


def _signatured_method_qn(
    method_node: Node, class_qn: str, language: cs.SupportedLanguage
) -> str | None:
    if language == cs.SupportedLanguage.JAVA:
        method_info = java_utils.extract_method_info(method_node)
        if method_name := method_info.get(cs.KEY_NAME):
            parameters = method_info.get(cs.KEY_PARAMETERS, [])
            param_sig = f"({','.join(parameters)})" if parameters else cs.EMPTY_PARENS
            return f"{class_qn}.{method_name}{param_sig}"
    elif language == cs.SupportedLanguage.CSHARP:
        # Give C# methods/constructors a parameter signature so
        # overloads and overloaded constructors stay distinct nodes
        # (without it two `Widget(...)` ctors collide and the second
        # gets an `@line` suffix). Zero-arg members stay bare so their
        # qn is stable and matches an unsignatured call site.
        cs_name, cs_params = csharp_utils.extract_method_signature(method_node)
        if cs_name and cs_params:
            param_sig = cs.SEPARATOR_COMMA_SPACE.join(cs_params)
            return f"{class_qn}.{cs_name}({param_sig})"
    return None


def _python_member_aliases(
    body_node: Node, language: cs.SupportedLanguage
) -> dict[int, tuple[str, ...]]:
    # Method node id -> the other member names the class body binds it to
    # (`run = _plain`, issue #2620), so the method registers under them too.
    if language != cs.SupportedLanguage.PYTHON:
        return {}
    names: dict[int, list[str]] = {}
    for alias, methods in scan_class_body_aliases(body_node).members.items():
        for method in methods:
            names.setdefault(method.id, []).append(alias)
    return {method_id: tuple(aliases) for method_id, aliases in names.items()}


def _python_non_method_members(
    class_node: Node, language: cs.SupportedLanguage
) -> list[str]:
    # The names the class body binds to a value that is no method (`run =
    # None`): they hide a base's `run` from attribute lookup (issue #2620).
    if language != cs.SupportedLanguage.PYTHON:
        return []
    if (body := class_node.child_by_field_name(cs.FIELD_BODY)) is None:
        return []
    return sorted(scan_class_body_aliases(body).non_method)


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
    bases and members. What waits for every type to be registered is held
    here and resolved by the `resolve_deferred_*` passes and the override
    walk."""

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
        "dart_annotated_overrides",
        "java_anon_overrides",
        "csharp_methods",
        "csharp_override_methods",
        "_csharp_partial_index",
        "csharp_type_locations",
        "go_type_locations",
        "go_interfaces",
        "cpp_definition_spans",
        "pending_field_types",
        "folded_overloads",
        "_deferred_parent_links",
        "_deferred_inherits",
        "cpp",
        "rust",
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
        self.dart_annotated_overrides = dart_annotated_overrides
        self.java_anon_overrides = java_anon_overrides
        self.csharp_methods = csharp_methods
        self.csharp_override_methods = csharp_override_methods
        self._csharp_partial_index = csharp_partial_index
        self.csharp_type_locations = csharp_type_locations
        self.go_type_locations = go_type_locations
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

    def reserve_python_class_qns(
        self,
        combined_captures: dict[str, list] | None,
        module_qn: str,
        lang_config: LanguageSpec,
        file_path: Path | None,
    ) -> None:
        # A file's functions and methods register before its classes, so a
        # `def Tool` shim written below `class Tool` took the plain name the
        # class is owed as the first definition (issue #2621). Only a class
        # sharing its name with a def can lose it, which spares every other
        # class a second identity pass (and a second truncated-name warning).
        reservations: dict[str, tuple[int, int]] = {}
        captures = combined_captures or {}
        def_names = {
            safe_decode_text(func_node.child_by_field_name(cs.FIELD_NAME))
            for func_node in captures.get(cs.CAPTURE_FUNCTION, [])
        }
        for class_node in captures.get(cs.CAPTURE_CLASS, []):
            name = safe_decode_text(class_node.child_by_field_name(cs.FIELD_NAME))
            if name is None or name not in def_names:
                continue
            identity = id_.resolve_class_identity(
                class_node,
                module_qn,
                cs.SupportedLanguage.PYTHON,
                lang_config,
                file_path,
            )
            if identity is None:
                continue
            class_qn = identity[0]
            start = _class_start_point(class_node, cs.SupportedLanguage.PYTHON)
            reservations[class_qn] = min(reservations.get(class_qn, start), start)
        self.function_registry.reserve_qns(reservations)

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
            scoped = self._resolve_csharp_scoped_base(entry)
            resolved = (
                (scoped, False)
                if scoped is not None
                else self._csharp_base_by_arity(
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
        self._flag_dart_external_overrides(dart_implements)
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

    def _flag_dart_external_overrides(
        self,
        implements_map: dict[str, list[str]],
    ) -> None:
        # Every Dart class ultimately extends Object, so an @override whose
        # name NO registered ancestor defines can only target external code:
        # a framework base (direct or through any chain of first-party
        # classes), an implemented external interface, or Object itself
        # (toString/hashCode/operator==, invoked by interpolation and
        # collections). Those methods root the dead-code walk; a name a
        # registered ancestor defines resolves via OVERRIDES edges and must
        # stay an ordinary candidate. Runs here because ancestry is only
        # known after every class is registered; the partial row MERGEs into
        # the node ingested during Pass 2.
        for child_qn, methods in self.dart_annotated_overrides.items():
            for method_qn, method_name in methods:
                if self._registered_ancestor_defines(
                    child_qn, method_name, implements_map
                ):
                    continue
                self.ingestor.ensure_node_batch(
                    cs.NodeLabel.METHOD,
                    {
                        cs.KEY_QUALIFIED_NAME: method_qn,
                        cs.KEY_OVERRIDES_EXTERNAL: True,
                    },
                )

    def _registered_ancestor_defines(
        self,
        class_qn: str,
        name: str,
        implements_map: dict[str, list[str]],
    ) -> bool:
        seen: set[str] = set()
        stack = list(self.facts.class_inheritance.get(class_qn, []))
        stack.extend(implements_map.get(class_qn, []))
        while stack:
            ancestor = stack.pop()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            if self.function_registry.get(ancestor) is None:
                continue
            if (
                self.function_registry.get(f"{ancestor}{cs.SEPARATOR_DOT}{name}")
                is not None
            ):
                return True
            stack.extend(self.facts.class_inheritance.get(ancestor, []))
            stack.extend(implements_map.get(ancestor, []))
        return False

    def _resolve_csharp_scoped_base(self, entry: DeferredInherit) -> str | None:
        if entry.written_ref is None:
            return None
        return bt.resolve_csharp_scoped_base(
            entry.written_ref,
            entry.child_qn,
            self.facts.csharp_class_namespaced.get(entry.child_qn),
            self.import_processor.import_mapping.get(entry.module_qn, {}),
            bt.CSharpTypeIndex(
                self.function_registry,
                self.facts.csharp_namespaced_qns,
                self.facts.csharp_class_generic_arity,
                self.facts.csharp_partial_groups,
            ),
        )

    def _js_reexport_target(self, entry: DeferredInherit) -> str | None:
        """The class a JS/TS base reaches through its module's exports.

        `export { Animal as AnimalBase }` publishes the class under a name
        nothing is registered under, so `extends AnimalBase` imported from
        that module names `base.AnimalBase` (issue #2894). The export table
        says which binding the name stands for, through any barrels that
        rename it again; only what a class can extend (a type, or in JS a
        constructor function) is a base at the end.
        """
        if entry.language not in cs.JS_TS_LANGUAGES:
            return None
        followed = self.import_processor.js_ts.follow_js_reexports(
            entry.parent_qn, self.module_qn_to_file_path, self.function_registry
        )
        # A subclass may take the base's declared name (hono's `Hono extends
        # HonoBase`); a chain ending on the child itself names no base.
        if followed != entry.child_qn and self.function_registry.get(
            followed
        ) in bt.base_target_kinds(entry.language):
            return followed
        return None

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
        if (followed := self._js_reexport_target(entry)) is not None:
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
        if (bound := self._js_ts_namespace_member(raw_name, entry)) is not None:
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

    def _js_ts_namespace_member(
        self, raw_name: str, entry: DeferredInherit
    ) -> str | None:
        # `implements r.Plain` or `extends r.Base` after `import * as r from
        # './router'` (issue #2560): the head is an import binding, so the
        # member is declared in the module it names, which outranks the name
        # sweep below. Only a registered type answers. A head bound outside
        # the project (`React` from a default import is `react.default`)
        # registers nothing, so `React.Component` keeps the written name it is
        # externalized under, which React dead-code rooting matches on.
        if entry.language not in cs.JS_TS_LANGUAGES:
            return None
        candidate = pe.js_ts_namespace_member_qn(
            raw_name, self.import_processor.import_mapping.get(entry.module_qn, {})
        )
        if candidate is None:
            return None
        # `r` is often a barrel (`export { Router } from './router'`): the
        # member is what the barrel EXPORTS under that name, followed on to
        # its declaring module. A name the barrel only imports for its own
        # use is no member: tsc rejects `implements r.Router` then.
        member = self.import_processor.js_ts.follow_js_reexports(
            candidate, self.module_qn_to_file_path, self.function_registry
        )
        if (
            member == entry.child_qn
            or self.function_registry.get(member)
            not in bt.base_target_kinds(entry.language)
            # The module's own declaration is a member only when exported.
            or (
                member == candidate and self.function_registry.is_module_private(member)
            )
        ):
            return None
        return member

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
        if (sibling := self._csharp_arity_sibling(entry)) is not None:
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

    def _csharp_arity_index(self) -> CSharpArityIndex:
        return CSharpArityIndex(
            self.function_registry,
            self.facts.csharp_class_generic_arity,
            self.facts.csharp_class_namespaced,
            self.facts.csharp_namespaced_qns,
            self.facts.csharp_partial_groups,
        )

    def _csharp_base_by_arity(
        self, entry: DeferredInherit, resolved: tuple[str, bool] | None
    ) -> tuple[str, bool] | None:
        # The name lookup finds `PB` or `PB<T>`, whichever it meets first; the
        # base's written arity picks the one C# binds (issue #2579). Only a C#
        # base carries a written ref.
        if resolved is None or resolved[1] or entry.written_ref is None:
            return resolved
        arity = csharp_utils.split_type_ref(entry.written_ref)[1]
        twin = self._csharp_arity_index().twin(resolved[0], arity)
        # The child is never its own base, whichever twin the name found.
        return (twin, False) if twin != entry.child_qn else resolved

    def _csharp_arity_sibling(self, entry: DeferredInherit) -> str | None:
        # Only C# overloads type names by generic arity, so only there can a
        # base that resolves to the declaring type itself legally name a
        # DIFFERENT type. The sibling conventionally lives beside the child
        # (Polly's Foo.cs + Foo.TResult.cs), so only a UNIQUE same-simple-name
        # type declaration under the module's parent package qualifies;
        # ambiguity keeps the no-edge answer rather than guessing.
        if entry.language != cs.SupportedLanguage.CSHARP:
            return None
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
        # The written arity names the sibling (issue #764): a twin in the
        # same file carries it in its qn (`Foo`1`, issue #2579), one in
        # another file shares the child's declared form.
        if entry.written_ref is not None:
            arity = csharp_utils.split_type_ref(entry.written_ref)[1]
            twin = self._csharp_arity_index().twin(entry.child_qn, arity)
            if twin != entry.child_qn:
                return twin
        natural = qn_markers.strip_arity_marker(qn_markers.natural_qn(entry.child_qn))
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
            return self._csharp_single_partial_representative(in_package)
        # No same-package sibling: the pair can span projects (Polly's
        # legacy BrokenCircuitException<TResult> : the Polly.Core
        # non-generic), so fall back to a project-wide unique declaration.
        return candidates.pop() if len(candidates) == 1 else None

    def _csharp_single_partial_representative(self, in_package: set[str]) -> str | None:
        # Multiple same-package candidates can still be ONE type: a
        # `partial` sibling split across files (Polly's PredicateBuilder,
        # issue #764 shape 3). When every candidate sits in a single
        # partial group, pick the deterministic representative; genuine
        # distinct types keep the no-edge answer.
        groups = {
            frozenset(self.facts.csharp_partial_groups.get(qn, (qn,)))
            for qn in in_package
        }
        if len(groups) == 1:
            return min(in_package)
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
        if non_method_members := _python_non_method_members(class_node, language):
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
            self._record_php_namespace(class_node, class_props)
        elif language == cs.SupportedLanguage.CSHARP:
            self._record_csharp_namespace(class_node, class_qn, class_props)
            self._record_csharp_arity(class_node, class_name, class_props)
        elif language == cs.SupportedLanguage.GO and label == cs.NodeLabel.INTERFACE:
            # Stored so an incremental run that does not re-parse the
            # interface still knows what its method set is built from.
            class_props.update(go_ifaces.embeds_props(class_node))
        elif language == cs.SupportedLanguage.PYTHON and under_type_checking(
            class_node
        ):
            # A checker-only class types `self` in mixins whose calls resolve
            # to the real implementation, so no edge ever reaches it; only the
            # parse can tell dead code it is a declaration.
            class_props[cs.KEY_TYPE_CHECKING_ONLY] = True

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
            self._record_dart_heritage(member_node, class_qn, module_qn)

    def _record_dart_heritage(
        self, member_node: Node, class_qn: str, module_qn: str
    ) -> None:
        if type_args := pe.extract_dart_extends_type_args(
            member_node,
            module_qn,
            partial(self._resolve_to_qn, language=cs.SupportedLanguage.DART),
        ):
            self.facts.dart_extends_type_args[class_qn] = type_args
        if on_type := dart_utils.extension_on_type(member_node):
            self.facts.dart_extension_on_types[class_qn] = on_type

    def _record_php_namespace(
        self, class_node: Node, class_props: PropertyDict
    ) -> None:
        # A PHP class's qn follows its path, but other files `use` it by the
        # namespace around its declaration; the dead-code walk needs that name
        # to tell an imported project base from a vendor class of the same
        # name (issue #2472).
        if namespace := php_namespaces.enclosing_namespace(class_node):
            class_props[cs.KEY_NAMESPACE] = namespace

    def _record_csharp_namespace(
        self, class_node: Node, class_qn: str, class_props: PropertyDict
    ) -> None:
        # The declared namespace is the type's own property, whether or
        # not the qn repeats it (issue #1629).
        if namespace := csharp_utils.declared_namespace(class_node):
            class_props[cs.KEY_NAMESPACE] = namespace
        if namespaced := csharp_utils.namespace_qualified_name(class_node):
            self.facts.csharp_class_namespaced[class_qn] = namespaced
            self.facts.csharp_namespaced_qns.setdefault(namespaced, set()).add(class_qn)

    def _record_csharp_arity(
        self, class_node: Node, class_name: str, class_props: PropertyDict
    ) -> None:
        # A generic twin's qn spells its CLR arity (`PB`1`, issue #2579), but
        # its name is still the one written. The simple-name index keeps the
        # qn's spelling, as the registry does for a type read back from the
        # graph, so a plain `PB` meets the non-generic twin only and a
        # written arity reaches the other. The arity is stored as well: an
        # incremental run knows a type in an unchanged file only from its
        # node, and `new PB<int>()` must find the same twin either way.
        class_props[cs.KEY_NAME] = qn_markers.strip_arity_marker(class_name)
        if arity := csharp_utils.type_parameter_count(class_node):
            class_props[cs.KEY_GENERIC_ARITY] = arity

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
        csharp_base_kinds: dict[str, str] | None = None
        if language == cs.SupportedLanguage.CSHARP and file_path is not None:
            rel_path = cached_relative_path(file_path, self.repo_path).as_posix()
            # Reverse index for the Roslyn frontend's location-keyed facts:
            # partial declaration groups join back to these Class qns after
            # Pass 2.
            self.csharp_type_locations[(rel_path, class_start_line)] = class_qn
            csharp_base_kinds = self.facts.frontend_facts[
                cs.SupportedLanguage.CSHARP
            ].base_kinds.get((rel_path, class_start_line))
        elif language == cs.SupportedLanguage.GO and file_path is not None:
            # Reverse index for the go/types frontend's implements facts: each
            # ImplementsPair end (a declaring identifier position) joins back to
            # this type's qn + node label after Pass 2. A Go type_spec's
            # start_point IS the name token, so the position matches directly --
            # no name alias (unlike Go funcs, keyed at the func keyword).
            rel_path = cached_relative_path(file_path, self.repo_path).as_posix()
            self.go_type_locations[(rel_path, class_start_line, class_start_col)] = (
                class_qn,
                str(label),
            )
        return csharp_base_kinds

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
            # Record Go struct field types so a field-hop receiver
            # (`engine.trees.get()`) resolves, and a local bound from such a call
            # (`root := engine.trees.get(m)`) picks up the return type.
            if field_types := GoTypeInferenceEngine().build_field_type_map(class_node):
                self.facts.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.RUST:
            self.rust.record_field_types(class_node, class_qn)
        elif language == cs.SupportedLanguage.DART:
            # Record Dart field types (`Greeter buddy;`) so a field-typed
            # receiver (`buddy.greet()`, `this.buddy.hail()`) resolves
            # through the field's declared type.
            if field_types := DartTypeInferenceEngine().build_field_type_map(
                class_node
            ):
                self.facts.class_field_types[class_qn] = field_types
        elif language == cs.SupportedLanguage.CSHARP:
            self._record_csharp_class_members(
                member_node, class_qn, module_qn, modifiers, file_path
            )

    def _record_csharp_class_members(
        self,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        modifiers: list[str],
        file_path: Path | None,
    ) -> None:
        # Record C# field/property types so a field-typed receiver
        # (`_w.M()`, `this._w.M()`) resolves, including a field inherited
        # from a base class in another file (the resolver walks
        # class_inheritance over these per-class maps).
        field_types = csharp_utils.build_field_type_map(member_node) or {}
        if member_node.type == cs.TS_CSHARP_RECORD_DECLARATION:
            _csharp_record_parameter_types(member_node, field_types)
        if field_types:
            self.facts.class_field_types[class_qn] = field_types
        # Declared type-parameter count: `Builder<TResult>` -> 1;
        # unrecorded means 0, so only generics are stored.
        arity = csharp_utils.type_parameter_count(member_node)
        if arity:
            self.facts.csharp_class_generic_arity[class_qn] = arity
            # The declaring module, recorded because the class qn
            # cannot yield it: a namespace pushes the qn under a
            # sibling module's qn (#1769).
            self.facts.csharp_class_owner_module[class_qn] = module_qn
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
        # The directory comes from the file's own segment by name: a part
        # with a dotted stem (`Widget.Designer.cs`) split from its sibling
        # under a last-dot rule, and once base resolution refused any
        # ambiguity that is not one partial group, the split lost the
        # `: N.Widget` edge (#1999).
        if cs.TS_CSHARP_MODIFIER_PARTIAL in modifiers:
            self._group_csharp_partial(class_qn, module_qn, file_path, arity)

    def _group_csharp_partial(
        self, class_qn: str, module_qn: str, file_path: Path | None, arity: int
    ) -> None:
        directory = module_directory_qn(module_qn, file_path) or module_qn
        # A second same-name part in ONE file registers under a
        # duplicate-suffixed qn (`Bench@24`); the marker is a
        # registration artefact, not part of the declared name, so
        # strip it or the two parts never share a group (issue #2014).
        # The tail below the module, so only its END can carry the
        # marker a registration appended; a verbatim identifier
        # (`@event`) opens with the same character and is kept.
        # The arity is always spelled: `Box<T>` beside a `Box` is `Box`1`
        # while its part in another file is plain `Box`, and each must join
        # the other, never the non-generic twin (issue #2579).
        declared = qn_markers.with_leaf_arity(class_qn[len(module_qn) + 1 :], arity)
        key = f"{directory}{cs.SEPARATOR_DOT}{declared}"
        group = self._csharp_partial_index.setdefault(key, [])
        group.append(class_qn)
        self.facts.csharp_partial_groups[class_qn] = group

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
            self._pin_csharp_base_module(entry, parent_qn)
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

    def _pin_csharp_base_module(self, entry: DeferredInherit, parent_qn: str) -> None:
        # A resolved first-party base pins the C# namespace import to the
        # module defining it (issue #1347).
        target_module = module_qn_for_entity(parent_qn, self.module_qn_to_file_path)
        if target_module is not None and target_module != entry.module_qn:
            self.import_processor.csharp.record_resolved_cross_module_use(
                entry.module_qn, target_module
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
        member_aliases = _python_member_aliases(body_node, language)

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
        # Names defined by an EXTERNAL stdlib base of this class (click's
        # `class TextWrapper(textwrap.TextWrapper)`): a method matching one
        # overrides the base and is invoked by its machinery, so mark it for
        # the dead-code root property. Only unregistered (external) parents
        # contribute; first-party bases resolve via OVERRIDES edges.
        external_override_names: frozenset[str] = frozenset()
        annotated_override_sink: dict[str, list[tuple[str, str]]] | None = None
        if language == cs.SupportedLanguage.PYTHON:
            external_parents = [
                p
                for p in self.facts.class_inheritance.get(class_qn, [])
                if self.function_registry.get(p) is None
            ]
            if external_parents:
                external_override_names = external_stdlib_base_method_names(
                    external_parents
                )
        elif language == cs.SupportedLanguage.DART:
            # An external base (a Flutter widget class) is not introspectable
            # the way Python stdlib bases are, but Dart marks every override
            # explicitly with @override. Whether a parent is truly external is
            # unknowable here (it may live in a file parsed later), so only
            # RECORD the annotated methods; resolve_deferred_inherits decides
            # once every class is registered.
            annotated_override_sink = self.dart_annotated_overrides
        return external_override_names, annotated_override_sink

    def _record_ingested_method(
        self, scope: _MethodScope, method_node: Node, ingested_qn: str
    ) -> None:
        language = scope.language
        module_qn = scope.module_qn
        if language == cs.SupportedLanguage.CSHARP:
            self.facts.csharp_call_shapes[ingested_qn] = csharp_call_shape(method_node)
            if (
                method_node.child_by_field_name(cs.TS_CSHARP_FIELD_TYPE_PARAMETERS)
                is not None
            ):
                self.facts.csharp_generic_methods.add(ingested_qn)
        # Record Dart return types (a constructor "returns" its class)
        # so a local bound from a static factory or named constructor
        # (`var s = Greeter.create()`) types from the RECORDED return
        # instead of guessing the class from the call's base name.
        if language == cs.SupportedLanguage.DART and (
            dart_return := dart_utils.return_type_name(method_node)
        ):
            self.facts.method_return_types[ingested_qn] = dart_return
            if method_node.type in cs.DART_CONSTRUCTOR_SIGNATURE_TYPES:
                self.facts.dart_constructor_qns.add(ingested_qn)
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
        # Track C# methods (and the `override`-modified subset) so the
        # override walk gates class-parent matches: an implicit hide or a
        # `new` shadow is not an override, unlike an interface impl.
        if language == cs.SupportedLanguage.CSHARP:
            self._track_csharp_method(scope, method_node, ingested_qn)
        # A Java method declared inside an anonymous class body
        # (`new Base(){ @Override m(){} }`) is ingested here under the enclosing
        # class but really overrides the anon class's base type. Record it so a
        # deferred pass emits the OVERRIDES edge once the base is registered;
        # with override-reachability that keeps the dispatch-only override live.
        if (
            language == cs.SupportedLanguage.JAVA
            and module_qn is not None
            and (base := _java_anonymous_base_type(method_node, scope.class_node))
        ):
            method_name = ingested_qn.rsplit(cs.SEPARATOR_DOT, 1)[-1].split(
                cs.CHAR_PAREN_OPEN, 1
            )[0]
            self.java_anon_overrides.append((ingested_qn, method_name, base, module_qn))
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
        if language == cs.SupportedLanguage.CSHARP:
            self._record_csharp_method_return_type(method_node, ingested_qn)

    def _track_csharp_method(
        self, scope: _MethodScope, method_node: Node, ingested_qn: str
    ) -> None:
        if scope.module_qn is not None:
            # Record where this member landed so the Roslyn frontend's
            # declaration-location facts (call targets, query callers)
            # resolve to the exact registered qn and label.
            self.facts.function_locations[
                function_span_key(scope.module_qn, method_node)
            ] = FunctionLocation(
                label=cs.NodeLabel.METHOD.value,
                qualified_name=ingested_qn,
                container_qn=scope.class_qn,
            )
        self.csharp_methods.add(ingested_qn)
        if csharp_has_override_modifier(method_node):
            self.csharp_override_methods.add(ingested_qn)
        # Index extension methods by simple name + receiver type so a
        # `recv.Ext()` call binds to the static method even though it
        # lives on an unrelated static class (not in recv's hierarchy).
        csharp_utils.index_extension_method(
            self.facts.csharp_extension_methods, ingested_qn, method_node
        )

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
            self._register_java_name_alias(module_qn, method_node, location)

    def _record_csharp_method_return_type(
        self, method_node: Node, ingested_qn: str
    ) -> None:
        if (
            rt_node := method_node.child_by_field_name(cs.TS_CSHARP_FIELD_RETURNS)
            # property_declaration exposes its type via `type`,
            # not `returns`; recording it lets chained typing and
            # the external-member gate see through properties.
            or method_node.child_by_field_name(cs.FIELD_TYPE)
        ) is None:
            return
        # Record a C# method's return type so a chained call
        # (`Policy.Handle<T>().CircuitBreaker(...)`, Polly's whole
        # fluent surface) can type the receiver for the next hop.
        if rt_text := csharp_utils.normalize_csharp_type_name(rt_node):
            raw = safe_decode_text(rt_node) or rt_text
            self.facts.csharp_method_return_types[ingested_qn] = (
                rt_text,
                csharp_utils.generic_arity_of_type_text(raw),
            )

    def _register_java_name_alias(
        self, module_qn: str, method_node: Node, location: FunctionLocation
    ) -> None:
        # The javac frontend (issue #1181) keys a call target at the callee NAME
        # token, but function_span_key keys at the declaration start -- the
        # modifiers, so `    public String handle(` records col 4 while the tool
        # reports the col of `handle`. Register a SECOND location at the name so
        # the semantic join resolves, mirroring the Go alias.
        # setdefault, not assignment: two methods sharing a line could put this
        # alias on another method's span key, and the span record is the
        # authoritative one.
        name_node = method_node.child_by_field_name(cs.FIELD_NAME)
        if name_node is None:
            return
        self.facts.function_locations.setdefault(
            (module_qn, name_node.start_point[0] + 1, name_node.start_point[1]),
            location,
        )

    def process_all_method_overrides(self, module_qns: Collection[str] = ()) -> None:
        # Every module the graph holds, read-back ones included: a C++ type
        # name is looked up by its path within its module (issue #2455).
        mo.process_all_method_overrides(
            self.function_registry,
            self.facts.class_inheritance,
            self.ingestor,
            self._override_walk_implementers(),
            self.csharp_methods,
            self.csharp_override_methods,
            self.rust.impl_method_traits,
            self.rust.inherent_impl_methods,
            module_qns={*self.module_qn_to_file_path, *module_qns},
            linked_apart=self.go_interfaces.keys(),
        )
        self._resolve_java_anon_overrides()

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

    def _resolve_java_anon_overrides(self) -> None:
        # Emit OVERRIDES edges for Java anonymous-class methods recorded at ingestion
        # (`new Base(){ @Override m(){} }`). The base type is resolved by UNIQUE
        # global simple-name match among type declarations (the base is usually in
        # another file; a full import resolve is unnecessary and this stays
        # revive-only; an ambiguous or unfound base is skipped). Each base method
        # directly on the base whose simple name matches gets an OVERRIDES edge from
        # the anon override, so override-reachability keeps the dispatch-only method
        # live when the base is reachable.
        type_decls = (cs.NodeLabel.CLASS, cs.NodeLabel.INTERFACE, cs.NodeLabel.ENUM)
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
            anon_label = anon_type
            base_candidates = [
                qn
                for qn in self.function_registry.find_ending_with(base_type)
                if self.function_registry.get(qn) in type_decls
            ]
            if len(base_candidates) != 1:
                continue
            base_qn = base_candidates[0]
            base_prefix = f"{base_qn}{cs.SEPARATOR_DOT}"
            for qn, label in self.function_registry.find_with_prefix(base_qn):
                if (
                    label == cs.NodeLabel.METHOD
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

    def _resolve_class_name(
        self,
        class_name: str,
        module_qn: str,
        language: cs.SupportedLanguage | None = None,
    ) -> str | None:
        resolved = self._resolve_base_name(class_name, module_qn, language)
        if resolved is not None or language != cs.SupportedLanguage.CSHARP:
            return resolved
        # A namespace-qualified C# name (`Zeta.BaseC` in a base list) used to
        # match the tail of the class qn; a namespace the directory spells is
        # no longer in it, so the declared form is looked up instead, for a
        # C# reference only: a Python `Zeta.BaseC` is not this index's
        # business (issue #1629).
        return csharp_utils.unique_carrier(
            self.facts.csharp_namespaced_qns.get(class_name),
            self.facts.csharp_partial_groups,
        )

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        return rel.get_node_type_for_inheritance(qualified_name, self.function_registry)
