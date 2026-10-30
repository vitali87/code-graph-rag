from __future__ import annotations

from collections.abc import Collection, Mapping
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from tree_sitter import Node, QueryCursor

from ... import constants as cs
from ...language_spec import LANGUAGE_SPECS, LanguageSpec
from ...node_text import sorted_captures
from ...types_defs import DeferredInherit, PropertyDict
from ...utils.path_utils import cached_relative_path, cached_resolve_posix
from ..anchor_hash import anchor_hash_props
from ..cpp import class_ingest as cpp_class_ingest
from ..csharp import class_ingest as csharp_class_ingest
from ..dart import class_ingest as dart_class_ingest
from ..enum_variants import emit_declared_variants
from ..field_nodes import PendingFieldType, emit_declared_fields
from ..go import class_ingest as go_class_ingest
from ..java import class_ingest as java_class_ingest
from ..js_ts import class_ingest as js_ts_class_ingest
from ..js_ts.utils import mark_js_ts_module_private
from ..language_definitions import LanguageClassIngest
from ..language_map import by_language
from ..method_ingest import MethodIngestHost
from ..php import class_ingest as php_class_ingest
from ..python import class_ingest as python_class_ingest
from ..python import resolve_class_name
from ..rust import class_ingest as rust_class_ingest
from ..scala import class_ingest as scala_class_ingest
from ..utils import decorated_start_props, extract_modifiers_and_decorators
from . import base_targets as bt
from . import definition_label as nt
from . import identity as id_
from . import method_override as mo
from . import relationships as rel
from .class_methods import ClassMethods, MethodScope
from .deferred_inherits import DeferredInherits

if TYPE_CHECKING:
    from ...types_defs import DeferredParentLink, LanguageQueries
    from ..definition_context import DefinitionContext, LanguageStates
    from ..function_ingest import FunctionIngest


class _WrittenClass(NamedTuple):
    # A class node as written: the qn it registered under, its label and
    # modifiers, and the properties its members inherit their path from.
    qn: str
    label: cs.NodeLabel
    is_exported: bool
    modifiers: list[str]
    props: PropertyDict
    start_line: int
    start_col: int


class ClassIngest:
    """Writes every class-like node a file's class captures yield (classes,
    interfaces, enums, traits, impl blocks, inline modules) with their methods,
    bases and members. The INHERITS/IMPLEMENTS edges that wait for every type
    to be registered are held here and resolved by `resolve_deferred_inherits`
    (`DeferredInherits`) and the override walk; a class body's methods are
    written by `ClassMethods`. A language's own share is the collaborator it
    builds from that language's package (`CppClassIngest`, `RustClassIngest`,
    `CSharpClassIngest` and the rest), which holds what that language defers
    until its own `resolve_deferred_*` pass and answers the `LanguageClassIngest`
    hooks this pass looks it up by language to call."""

    __slots__ = (
        "_method_host",
        "_function_ingest",
        "ingestor",
        "repo_path",
        "function_registry",
        "simple_name_lookup",
        "module_qn_to_file_path",
        "import_processor",
        "facts",
        "class_owner_module",
        "pending_field_types",
        "_deferred_parent_links",
        "_deferred_inherits",
        "_inherits",
        "_methods",
        "cpp",
        "c",
        "rust",
        "csharp",
        "go",
        "dart",
        "js_ts",
        "java",
        "python",
        "php",
        "scala",
        "_languages",
        "_neutral",
        "_unwalked_read_back_suffixes",
    )

    def __init__(
        self,
        *,
        method_host: MethodIngestHost,
        function_ingest: FunctionIngest,
        context: DefinitionContext,
        states: LanguageStates,
        flow_capture_enabled: bool,
        class_owner_module: dict[str, str],
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
        self.ingestor = context.ingestor
        self.repo_path = context.repo_path
        self.function_registry = context.function_registry
        self.simple_name_lookup = context.simple_name_lookup
        self.module_qn_to_file_path = context.module_qn_to_file_path
        self.import_processor = context.import_processor
        self.facts = context.facts
        self.class_owner_module = class_owner_module
        self.pending_field_types = pending_field_types
        # Shared with the function pass, which emits them once every parent
        # is registered.
        self._deferred_parent_links = deferred_parent_links
        # Non-C++ INHERITS/IMPLEMENTS held back until every class is
        # registered; resolve_deferred_inherits re-resolves the guesses.
        self._deferred_inherits: list[DeferredInherit] = []
        self._inherits = DeferredInherits(self, context)
        self._methods = ClassMethods(
            self,
            method_host=method_host,
            repo_path=context.repo_path,
            facts=context.facts,
            cpp_definition_spans=states.cpp.definition_spans,
            folded_overloads=folded_overloads,
        )
        self.cpp = cpp_class_ingest.CppClassIngest(
            self, function_ingest.cpp, context, states.cpp
        )
        self.c = cpp_class_ingest.CClassIngest(self.cpp)
        self.rust = rust_class_ingest.RustClassIngest(
            self,
            context,
            states.rust,
            method_host=method_host,
            flow_capture_enabled=flow_capture_enabled,
            deferred_parent_links=deferred_parent_links,
        )
        self.csharp = csharp_class_ingest.CSharpClassIngest(context, states.csharp)
        self.go = go_class_ingest.GoClassIngest(method_host, context, states.go)
        self.dart = dart_class_ingest.DartClassIngest(self, context, states.dart)
        self.js_ts = js_ts_class_ingest.JsTsClassIngest(context)
        self.java = java_class_ingest.JavaClassIngest(context, states.java)
        self.python = python_class_ingest.PythonClassIngest(
            context.function_registry, context.facts
        )
        self.php = php_class_ingest.PhpClassIngest()
        self.scala = scala_class_ingest.ScalaClassIngest(context.import_processor)
        self._languages = self._language_ingests()
        self._neutral = LanguageClassIngest()
        self._unwalked_read_back_suffixes = self._unwalked_suffixes()

    def _language_ingests(self) -> dict[cs.SupportedLanguage, LanguageClassIngest]:
        # The collaborator each language's class nodes go to; a language with
        # none takes the neutral answers.
        return by_language(
            {
                cs.SupportedLanguage.PYTHON: self.python,
                cs.JS_TS_LANGUAGES: self.js_ts,
                cs.SupportedLanguage.JAVA: self.java,
                cs.SupportedLanguage.RUST: self.rust,
                cs.SupportedLanguage.GO: self.go,
                cs.SupportedLanguage.CPP: self.cpp,
                cs.SupportedLanguage.C: self.c,
                cs.SupportedLanguage.PHP: self.php,
                cs.SupportedLanguage.CSHARP: self.csharp,
                cs.SupportedLanguage.DART: self.dart,
                cs.SupportedLanguage.SCALA: self.scala,
            }
        )

    def _unwalked_suffixes(self) -> tuple[str, ...]:
        # The files whose read-back implementer pairs the override walk
        # leaves out, by the suffixes of the languages that say so.
        return tuple(
            extension
            for language, language_ingest in self._languages.items()
            if not language_ingest.joins_read_back_implementers
            for extension in LANGUAGE_SPECS[language].file_extensions
        )

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

        class_nodes.extend(self._for(language).extra_class_nodes(root_node))

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
            self._for(language).ingest_inline_modules(
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
        emitted = self._inherits.resolve(deferred)
        self.dart.flag_external_overrides()
        self.rust.flag_external_trait_overrides()
        return emitted

    def _resolve_deferred_parent_qn(
        self, entry: DeferredInherit
    ) -> tuple[str, bool] | None:
        # Rust's impl blocks resolve their trait through the same chain
        # (`DeferredInherits.resolve_parent_qn`).
        return self._inherits.resolve_parent_qn(entry)

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
        language_ingest = self._for(language)
        if language_ingest.skips_class(
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
        local_qn = language_ingest.local_type_qn(class_node, module_qn)
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

        written = self._write_class_node(
            class_node, identity, module_qn, language, lang_queries, file_path
        )
        parent_label, parent_qn, parent_span = (
            self.cpp.local_type_parent(class_node, local_qn, module_qn)
            if local_qn is not None
            else self._function_ingest.determine_function_parent(
                class_node, written.qn, module_qn, lang_config, language
            )
        )
        self._function_ingest.emit_or_defer_defines(
            parent_label,
            parent_qn,
            written.label,
            written.qn,
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
        self._ingest_class_members(
            class_node,
            written,
            MethodScope(
                member_node,
                written.qn,
                written.label,
                language,
                file_path,
                module_qn,
            ),
            lang_queries,
            sorted_func_nodes,
            func_node_starts,
        )

    def _write_class_node(
        self,
        class_node: Node,
        identity: tuple[str, str, bool],
        module_qn: str,
        language: cs.SupportedLanguage,
        lang_queries: LanguageQueries,
        file_path: Path | None,
    ) -> _WrittenClass:
        # Register the class's qn, write its node and index it.
        language_ingest = self._for(language)
        class_qn, class_name, is_exported = identity
        class_start_line, class_start_col = language_ingest.class_start_point(
            class_node
        )
        label = nt.definition_label(class_node, class_name, class_qn, language)
        class_qn = self.function_registry.register_unique_qn(
            class_qn,
            class_start_line,
            class_start_col,
            kind=label if language_ingest.separate_declaration_spaces else None,
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
        language_ingest.record_type_props(
            class_node, class_qn, class_name, label, class_props
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
        non_method_members = language_ingest.non_method_members(class_node)
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
        return _WrittenClass(
            class_qn,
            label,
            is_exported,
            modifiers,
            class_props,
            class_start_line,
            class_start_col,
        )

    def _ingest_class_members(
        self,
        class_node: Node,
        written: _WrittenClass,
        scope: MethodScope,
        lang_queries: LanguageQueries,
        sorted_func_nodes: list[Node] | None,
        func_node_starts: list[int] | None,
    ) -> None:
        # What a written class holds, read off its member node: its declared
        # fields, its bases, the members its kind declares, its language's
        # own and its methods.
        member_node, language, file_path = (
            scope.class_node,
            scope.language,
            scope.file_path,
        )
        module_qn = scope.module_qn
        class_qn, label, class_props = written.qn, written.label, written.props
        language_ingest = self._for(language)
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
        csharp_base_kinds = (
            language_ingest.record_type_location(
                class_qn, label, file_path, written.start_line, written.start_col
            )
            if file_path is not None
            else None
        )
        rel.create_class_relationships(
            member_node,
            class_qn,
            module_qn,
            label,
            written.is_exported,
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
        language_ingest.record_members(
            class_node,
            member_node,
            class_qn,
            module_qn,
            label,
            written.modifiers,
            file_path,
        )
        self._methods.ingest(scope, lang_queries, sorted_func_nodes, func_node_starts)

    def _record_declared_type_members(
        self,
        member_node: Node,
        class_qn: str,
        module_qn: str,
        label: cs.NodeLabel,
        language: cs.SupportedLanguage,
        class_props: PropertyDict,
    ) -> None:
        language_ingest = self._for(language)
        # The variants ride with their enum the way fields ride with their
        # owner (issue #1807). After the relationships, because a Python enum
        # is a Class that only its resolved bases identify (issue #2583).
        if label == cs.NodeLabel.ENUM or language_ingest.is_enum(class_qn):
            emit_declared_variants(
                self.ingestor,
                label,
                class_qn,
                member_node,
                language,
                class_props,
                self.import_processor.import_mapping.get(module_qn),
            )
        language_ingest.record_heritage(member_node, class_qn, module_qn)

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
            linked_apart=self._overrides_linked_apart(),
        )
        self.java.resolve_anonymous_overrides()

    def _overrides_linked_apart(self) -> set[str]:
        # The types another pass links the overrides of, from every language
        # that has such a pass.
        return {
            qn
            for language_ingest in dict.fromkeys(self._languages.values())
            for qn in language_ingest.overrides_linked_apart()
        }

    def _override_walk_implementers(self) -> dict[str, set[str]]:
        # The walk visits a class's superclasses and interfaces together and
        # stops at the first that declares the method. With only the parsed
        # pairs, an unchanged class that extends one type and implements
        # another walked its superclasses alone and took an edge its own
        # parse never chose (issue #2403), so the pairs read back join them.
        # Not those of a language whose override edges something else decides
        # (`joins_read_back_implementers`), nor the parsed pairs of a type
        # another pass links the overrides of (`overrides_linked_apart`).
        linked_apart = self._overrides_linked_apart()
        merged = {
            qn: set(impls)
            for qn, impls in self.facts.interface_implementers.items()
            if qn not in linked_apart
        }
        for iface, implementers in self.facts.rehydrated_interface_implementers.items():
            if walked := {qn for qn in implementers if self._walks_read_back(qn)}:
                merged.setdefault(iface, set()).update(walked)
        return merged

    def _walks_read_back(self, implementer_qn: str) -> bool:
        # Read back from a file whose language joins its read-back pairs. No
        # recorded path means this run parsed the class (its parsed pairs
        # already cover it) or it is a Rust impl target with no node of its
        # own.
        path = self.facts.rehydrated_definition_paths.get(implementer_qn)
        return path is not None and not path.endswith(self._unwalked_read_back_suffixes)

    def _get_node_type_for_inheritance(self, qualified_name: str) -> str:
        return rel.get_node_type_for_inheritance(qualified_name, self.function_registry)
