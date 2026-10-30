from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..language_spec import has_other_language_sibling, own_extension_module_qn
from ..types_defs import (
    ASTNode,
    DeferredParentLink,
    FunctionRegistryTrieProtocol,
    PendingTypeFact,
    SimpleNameLookup,
)
from ..utils.path_utils import (
    base_module_qn,
    cached_relative_path,
    declaration_extension,
    has_implementation_sibling,
)
from .class_ingest import ClassIngest
from .cpp.definition_state import CppDefinitionState
from .cpp.file_ingest import CppFileIngest
from .csharp.definition_state import CSharpDefinitionState
from .dart.definition_state import DartDefinitionState
from .definition_context import DefinitionContext, LanguageStates
from .definition_docstring import extract_definition_docstring
from .definition_facts import DefinitionFacts
from .definition_prune import RemovedFile, forget_file
from .dependency_parser import read_manifest
from .field_nodes import PendingFieldType
from .file_parse import file_combined_captures, parse_file_root
from .function_ingest import FunctionIngest
from .go.builds import GoBuilds
from .go.definition_state import GoDefinitionState
from .go.file_ingest import GoFileIngest
from .go.link_state import GoLinkState
from .java.definition_state import JavaDefinitionState
from .js_ts.file_ingest import JsTsFileIngest
from .js_ts.ingest import JsTsIngest
from .js_ts.module_system import JsTsModuleSystem
from .js_ts.overloads import signature_docstring
from .julia.file_ingest import JuliaFileIngest
from .language_definitions import LanguageFileIngest
from .language_map import by_language
from .module_node import ModuleNodes
from .notebook import is_notebook_path
from .parameter_nodes import PendingParameterType
from .php.file_ingest import PhpFileIngest
from .python.docstring import python_docstring
from .python.file_ingest import PythonFileIngest
from .rust.definition_state import RustDefinitionState
from .rust.file_ingest import RustFileIngest
from .sql.file_ingest import SqlFileIngest

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .import_processor import ImportProcessor


class DefinitionProcessor:
    """The definition pass: each file's Module node and the classes,
    functions and imports in it, written for the later passes to resolve
    against.

    `process_file` parses a file (`file_parse.py`), settles its module qn
    and writes its module (`module_node.py`), hands its imports to the
    import pass and runs the class and function passes this pass builds,
    `ClassIngest` and `FunctionIngest`. A language's share of a file is the
    `LanguageFileIngest` collaborator its language looks up to. The pass
    holds the maps its passes share and the later passes read, the
    `resolve_deferred_*` entry points that settle what each pass held back
    until every file was parsed, and the external packages a manifest
    names."""

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        simple_name_lookup: SimpleNameLookup,
        import_processor: ImportProcessor,
        module_qn_to_file_path: dict[str, Path],
        func_class_captures_cache: dict[Path, dict] | None = None,
        *,
        flow_capture_enabled: bool = False,
        exclude_paths: frozenset[str] | None = None,
        unignore_paths: frozenset[str] | None = None,
    ):
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.import_processor = import_processor
        self.module_qn_to_file_path = module_qn_to_file_path
        self.flow_capture_enabled = flow_capture_enabled
        # The indexer's eligibility policy, needed by the declaration
        # tie-break: a declaration must yield only to an implementation that
        # will actually be indexed, not merely one that exists on disk.
        self.exclude_paths = exclude_paths
        self.unignore_paths = unignore_paths
        # The maps this pass fills for type inference and the call pass.
        self.facts = DefinitionFacts()
        # {class qn: module qn of the file that DECLARED it}, written for
        # EVERY language at class ingest. `facts.class_inheritance` and
        # `facts.class_field_types` are keyed by a class qn, and a class records
        # no function span, so the span-based ownership the registry sweep
        # uses cannot attribute one. Nor can a prefix rule: a C# class qn
        # embeds its namespace, so `proj/Core.cs` with `namespace Util`
        # yields `proj.Core.Util.Helper`, sitting under the SIBLING module
        # `proj.Core.Util` (#1769). This is the C#-only
        # `facts.csharp_class_owner_module`'s cross-language counterpart,
        # needed because both of those maps are written by every language's
        # ingest (#1772).
        self.class_owner_module: dict[str, str] = {}
        # {module qn: start bytes of the overload declarations that fold into
        # their implementation}: Python's `@overload` stubs (issue #2590) and
        # TypeScript's overload signatures (issue #2457). Both the function and
        # the method pass skip them so the implementation owns the name.
        self.folded_overloads: dict[str, frozenset[int]] = {}
        # Repo-relative prefixes of annotation-processor output (issue #1140):
        # modules under one are stamped generated with a processor hint.
        self.generated_source_prefixes: list[str] = []
        # Non-module DEFINES held back until every parent is registered,
        # written by the function and the class passes alike.
        self._deferred_parent_links: list[DeferredParentLink] = []
        # {manifest path: why its content could not be parsed}, for the one
        # WARNING the updater logs per run in place of a line per file.
        self.unparsable_manifests: dict[Path, str] = {}
        # Route-decorated handlers deferred from Pass 2: endpoint templates
        # need router mount prefixes, which may live in modules parsed later
        # (issue #877). (label, qn, route decorators, module_qn).
        self.pending_endpoints: list[
            tuple[cs.NodeLabel, str, list[str], str | None]
        ] = []
        # Return/parameter annotations awaiting the full registry (#1527).
        self.pending_type_facts: list[PendingTypeFact] = []
        self.pending_parameter_types: list[PendingParameterType] = []
        self.pending_field_types: list[PendingFieldType] = []
        self.func_class_captures_cache = func_class_captures_cache
        # Each language's own containers, which that language's
        # collaborators fill for a later pass of the language, the call pass
        # or the graph updater to read.
        self.cpp = CppDefinitionState()
        self.csharp = CSharpDefinitionState()
        self.dart = DartDefinitionState()
        self.go = GoDefinitionState(GoBuilds(repo_path))
        self.java = JavaDefinitionState()
        self.rust = RustDefinitionState()
        context = DefinitionContext(
            ingestor=ingestor,
            repo_path=repo_path,
            project_name=project_name,
            function_registry=function_registry,
            simple_name_lookup=simple_name_lookup,
            module_qn_to_file_path=module_qn_to_file_path,
            import_processor=import_processor,
            facts=self.facts,
        )
        states = LanguageStates(
            cpp=self.cpp,
            csharp=self.csharp,
            dart=self.dart,
            go=self.go,
            java=self.java,
            rust=self.rust,
        )
        self.function_ingest = FunctionIngest(
            method_host=self,
            context=context,
            states=states,
            folded_overloads=self.folded_overloads,
            pending_endpoints=self.pending_endpoints,
            pending_type_facts=self.pending_type_facts,
            pending_parameter_types=self.pending_parameter_types,
            deferred_parent_links=self._deferred_parent_links,
        )
        self.class_ingest = ClassIngest(
            method_host=self,
            function_ingest=self.function_ingest,
            context=context,
            states=states,
            flow_capture_enabled=flow_capture_enabled,
            class_owner_module=self.class_owner_module,
            pending_field_types=self.pending_field_types,
            folded_overloads=self.folded_overloads,
            deferred_parent_links=self._deferred_parent_links,
        )
        self.js_ts_module_system = JsTsModuleSystem(
            host=self, function_ingest=self.function_ingest, context=context
        )
        self.js_ts_ingest = JsTsIngest(
            host=self,
            function_ingest=self.function_ingest,
            context=context,
            class_owner_module=self.class_owner_module,
        )
        self._modules = ModuleNodes(
            self,
            ingestor=ingestor,
            repo_path=repo_path,
            project_name=project_name,
            flow_capture_enabled=flow_capture_enabled,
        )
        # Reached by name as well: the graph updater keeps the source of a SQL
        # file it re-reads through `keep_sql_source`.
        self._sql = SqlFileIngest(self.facts)
        self._languages = self._file_ingests()
        self._neutral = LanguageFileIngest()

    def _file_ingests(self) -> dict[cs.SupportedLanguage, LanguageFileIngest]:
        # The collaborator each language's files go to; a language with none
        # takes the neutral answers.
        return by_language(
            {
                cs.SupportedLanguage.PYTHON: PythonFileIngest(self.class_ingest.python),
                cs.JS_TS_LANGUAGES: JsTsFileIngest(
                    ingestor=self.ingestor,
                    import_processor=self.import_processor,
                    ingest=self.js_ts_ingest,
                    module_system=self.js_ts_module_system,
                    function_ingest=self.function_ingest.js_ts,
                ),
                cs.SupportedLanguage.RUST: RustFileIngest(
                    self.function_ingest.rust, self.import_processor, self.facts
                ),
                cs.SupportedLanguage.GO: GoFileIngest(self.facts),
                cs.SupportedLanguage.CPP: CppFileIngest(
                    self.class_ingest.cpp, self.facts
                ),
                cs.SupportedLanguage.JULIA: JuliaFileIngest(),
                cs.SupportedLanguage.PHP: PhpFileIngest(),
                cs.SupportedLanguage.SQL: self._sql,
            }
        )

    def _for(self, language: cs.SupportedLanguage) -> LanguageFileIngest:
        return self._languages.get(language, self._neutral)

    def begin_run(self) -> None:
        # What a run records about itself: a reused updater must not treat
        # a previous run's interfaces as unflushed writes rehydration has to
        # keep, nor report its unreadable manifests again.
        self.cpp.interfaces_parsed_this_run.clear()
        self.unparsable_manifests.clear()

    def go_link_state(self) -> GoLinkState:
        """The Go interface link's containers call resolution reads, by
        reference: the link refills them in place."""
        return self.go.link_state(self.facts.go_declaration_variants)

    def forget_file(self, removed: RemovedFile) -> None:
        """Drop what a removed or re-parsed file recorded in this pass's maps."""
        forget_file(
            self.facts, self.class_owner_module, self.rust.impl_owner_module, removed
        )

    def link_go_interfaces(
        self,
        analysed_roots: frozenset[str],
        checked_files: frozenset[str],
        compared: frozenset[str],
    ) -> int:
        """Link Go embedding, implicit satisfaction and the overrides it
        implies (`GoInterfaceIngest.link_go_interfaces`); returns the number of
        IMPLEMENTS edges written."""
        return self.class_ingest.go.interface_ingest.link_go_interfaces(
            analysed_roots, checked_files, compared
        )

    def replace_go_implementers(
        self,
        previous: set[tuple[str, str]],
        current: list[tuple[str, str]],
        keep: set[tuple[str, str]],
    ) -> None:
        """Swap one source's Go (interface, implementer) pairs for this
        pass's (`GoInterfaceIngest.replace_go_implementers`)."""
        self.class_ingest.go.interface_ingest.replace_go_implementers(
            previous, current, keep
        )

    def resolve_deferred_cpp_methods(self) -> int:
        """Ingest the out-of-class C++ methods held until every class is
        known (`CppFunctionIngest.resolve_deferred_methods`)."""
        return self.function_ingest.cpp.resolve_deferred_methods()

    def resolve_deferred_cpp_artifacts(self) -> int:
        """Decide the held-back artifact-shaped C++ nodes
        (`CppFunctionIngest.resolve_deferred_artifacts`)."""
        return self.function_ingest.cpp.resolve_deferred_artifacts()

    def resolve_deferred_cpp_prototypes(self) -> int:
        """Register the held-back C++ prototypes no definition duplicates
        (`CppFunctionIngest.resolve_deferred_prototypes`)."""
        return self.function_ingest.cpp.resolve_deferred_prototypes()

    def resolve_deferred_go_methods(self) -> int:
        """Ingest the Go receiver methods held until every receiver type is
        registered (`GoFunctionIngest.resolve_deferred_methods`)."""
        return self.function_ingest.go.resolve_deferred_methods()

    def resolve_deferred_parent_links(self) -> int:
        """Emit the non-module DEFINES held until every pass has registered
        (`FunctionIngest.resolve_deferred_parent_links`)."""
        return self.function_ingest.resolve_deferred_parent_links()

    def resolve_deferred_cpp_containment(self) -> int:
        """Emit DEFINES for functions nested in out-of-class C++ method bodies
        (`CppFunctionIngest.resolve_deferred_containment`)."""
        return self.function_ingest.cpp.resolve_deferred_containment()

    def resolve_deferred_cpp_module_impls(self) -> int:
        """Emit the C++ ModuleImplementation IMPLEMENTS edges whose interface
        exists (`CppClassIngest.resolve_deferred_module_impls`)."""
        return self.class_ingest.cpp.resolve_deferred_module_impls()

    def resolve_deferred_forward_declarations(self) -> int:
        """Drop or register the held-back C++ forward declarations
        (`CppClassIngest.resolve_deferred_forward_declarations`)."""
        return self.class_ingest.cpp.resolve_deferred_forward_declarations()

    def resolve_deferred_cpp_inherits(self) -> int:
        """Emit the C++ INHERITS edges held until every class is registered
        (`CppClassIngest.resolve_deferred_inherits`)."""
        return self.class_ingest.cpp.resolve_deferred_inherits()

    def resolve_deferred_inherits(self) -> int:
        """Emit the non-C++ INHERITS/IMPLEMENTS edges held until every class
        is registered (`ClassIngest.resolve_deferred_inherits`)."""
        return self.class_ingest.resolve_deferred_inherits()

    def process_all_method_overrides(self, module_qns: Collection[str] = ()) -> None:
        """Write the OVERRIDES edges of every method
        (`ClassIngest.process_all_method_overrides`)."""
        self.class_ingest.process_all_method_overrides(module_qns)

    def reset_interface_implementers(self) -> None:
        """Forget every implementer pair, parsed or read back from the graph.

        A full build re-parses every file and so re-records each pair it
        should hold, but a forced one skips the read-back that rebuilds the
        graph's pairs. A reused updater then kept an earlier run's pairs, and
        a deleted implementer still counted: the remaining one lost the
        sole-implementer edge a fresh build emits.
        """
        # The Go pairs in place: the call resolver holds them through the Go
        # link state.
        self.facts.interface_implementers.clear()
        self.facts.rehydrated_interface_implementers.clear()
        self.rust.impl_owner_module.clear()
        self.go.proven_pairs.clear()
        self.go.guessed_pairs.clear()

    def _disambiguate_module_qn(self, module_qn: str, file_path: Path) -> str:
        # A notebook always carries its extension (`proj.analysis.ipynb`). No
        # import can load it, so the bare name belongs to `analysis.py` or the
        # package `analysis/` whether or not one exists yet, and adding one
        # beside it never renames the notebook's definitions (issue #2480).
        if is_notebook_path(file_path):
            module_qn = own_extension_module_qn(module_qn, file_path.name)
        # A TypeScript declaration file and its implementation strip to the
        # SAME module qn once `.d.ts` is treated as one extension (#1720), and
        # the rule below would award it to whichever is walked first. That is
        # the stub: `"foo.d.ts" < "foo.ts"` in the ascending within-directory
        # walk. The type-only file would own the name every importer resolves
        # to while the callable definitions sat under `proj.foo.ts`, which
        # nothing asks for -- the regression that closed PR #1721.
        #
        # So a declaration yields whenever an implementation EXISTS ON DISK,
        # which is a fact about the repository rather than about parse order.
        # A declaration with no implementation still takes the bare name: for
        # `@types/*` packages and for a compiled library shipped beside its
        # types, that file IS the module, and an unconditional yield would put
        # it back under a name nothing resolves to.
        # Note this REWRITES the candidate rather than returning it: the
        # yielded name is itself a qn like any other and can collide with a
        # real module (`foo/d/ts.py` derives `proj.foo.d.ts`), so it still has
        # to go through the check below rather than skip it.
        #
        # A stem shared ACROSS language families (`util.py` beside `util.js`)
        # has no bare name: every file on it takes its extension (issue
        # #2586). Awarding it by walk order handed it to whichever file sorts
        # first, so adding `util.js` beside an indexed `util.py` re-pointed
        # `proj.util.helper`, and everything stored against it, at the JS
        # function. Names stay a function of the tree (#1569), so one of the
        # two must lose the name it had alone; retiring it for both is the one
        # rule under which an addition never hands a name to another file.
        # Importers still write the bare name and land on their own family's
        # file (ImportProcessor.point_imports_at_own_language_siblings).
        if has_other_language_sibling(
            file_path,
            self.repo_path,
            exclude_paths=self.exclude_paths,
            unignore_paths=self.unignore_paths,
        ):
            module_qn = own_extension_module_qn(module_qn, file_path.name)
        elif (declaration := declaration_extension(file_path.name)) and (
            has_implementation_sibling(
                file_path,
                self.repo_path,
                exclude_paths=self.exclude_paths,
                unignore_paths=self.unignore_paths,
            )
        ):
            suffix = declaration.lstrip(cs.SEPARATOR_DOT)
            module_qn = f"{module_qn}{cs.SEPARATOR_DOT}{suffix}"

        # Two files that share a basename but differ by extension (foo.py /
        # foo.cpp) strip to the same module qn. Append the extension to the
        # later one so their module nodes and all derived class/method qns stay
        # distinct instead of colliding under the qualified_name constraint.
        existing = self.module_qn_to_file_path.get(module_qn)
        if existing is None or existing == file_path:
            return module_qn
        return (
            f"{module_qn}{cs.SEPARATOR_DOT}{file_path.suffix.lstrip(cs.SEPARATOR_DOT)}"
        )

    def emit_type_edges(self) -> int:
        """Resolve queued return/parameter annotations into RETURNS/ACCEPTS.

        Runs once the registry holds every file's types (issue #1527); the
        queue empties, so a watch-mode re-parse only re-resolves its own.
        """
        from .type_facts import (
            TypeReferenceResolver,
            emit_of_type_edges,
            emit_type_edges,
        )

        # Every queue, not just the first: a Parameter or Field fact with no
        # RETURNS/ACCEPTS fact beside it would otherwise be skipped outright,
        # and OF_TYPE silently absent. A class whose only annotations are on
        # fields is the case that reaches this.
        if not (
            self.pending_type_facts
            or self.pending_parameter_types
            or self.pending_field_types
        ):
            return 0
        resolver = TypeReferenceResolver(
            self.function_registry,
            self.import_processor.import_mapping,
            self.project_name,
            self.facts.csharp_class_generic_arity,
        )
        resolver.join_partial_groups(self.facts.csharp_partial_groups)
        emitted = emit_type_edges(self.pending_type_facts, resolver, self.ingestor)
        # Parameter OF_TYPE edges resolve in the same pass, for the same
        # reason: the annotation may name a type from a later file.
        emitted += emit_of_type_edges(
            cs.NodeLabel.PARAMETER,
            self.pending_parameter_types,
            resolver,
            self.ingestor,
        )
        emitted += emit_of_type_edges(
            cs.NodeLabel.FIELD, self.pending_field_types, resolver, self.ingestor
        )
        return emitted

    def process_file(
        self,
        file_path: Path,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        structural_elements: dict[Path, str | None],
        source_bytes: bytes | None = None,
        pre_parsed: tuple[ASTNode, dict[str, list] | None] | None = None,
    ) -> tuple[ASTNode, cs.SupportedLanguage] | None:
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = cached_relative_path(file_path, self.repo_path)
        relative_path_str = relative_path.as_posix()
        logger.debug(
            ls.DEF_PARSING_AST.format(language=language, path=relative_path_str)
        )

        try:
            if language not in queries:
                logger.warning(
                    ls.DEF_UNSUPPORTED_LANGUAGE.format(
                        language=language, path=file_path
                    )
                )
                return None

            language_ingest = self._for(language)
            source_bytes = language_ingest.read_source(file_path, source_bytes)
            parsed = parse_file_root(
                file_path, language, queries, source_bytes, pre_parsed
            )
            if parsed is None:
                return None
            root_node, pre_combined_captures = parsed
            language_ingest.settle_parse(
                file_path, relative_path_str, root_node, source_bytes
            )

            module_qn = self._register_module(
                file_path, relative_path, language, root_node, structural_elements
            )

            combined_captures = file_combined_captures(
                self.func_class_captures_cache,
                file_path,
                root_node,
                language,
                queries,
                pre_combined_captures,
            )

            # A reused updater's second run re-parses this module into a map
            # still holding the first run's spans. The key is (module_qn,
            # line, col), so a function renamed in place keeps its key and the
            # first-claim guard would block the live registration, filing body
            # `use` imports under the dead qn (issue #1019).
            self.facts.function_locations.drop_module(module_qn)
            # Before the function pass below, which would otherwise take a
            # class's plain name first (issue #2621).
            language_ingest.reserve_names(
                combined_captures, module_qn, queries[language], file_path
            )

            self.import_processor.parse_imports(
                root_node,
                module_qn,
                language,
                queries,
                pre_captures=combined_captures,
            )
            language_ingest.ingest_after_imports(
                root_node, module_qn, language, queries, file_path
            )
            self._fold_overloads(
                root_node, module_qn, language, queries, combined_captures
            )
            self._ingest_definitions(
                root_node, module_qn, language, queries, combined_captures
            )
            language_ingest.finalise(root_node, module_qn, language, queries)

            return (root_node, language)

        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            logger.error(ls.DEF_PARSE_FAILED.format(path=file_path, error=e))
            return None

    def _register_module(
        self,
        file_path: Path,
        relative_path: Path,
        language: cs.SupportedLanguage,
        root_node: ASTNode,
        structural_elements: dict[Path, str | None],
    ) -> str:
        # The file's module qn, mapped back to the file and written as its
        # Module node.
        module_qn = base_module_qn(relative_path, self.project_name)
        module_qn = self._disambiguate_module_qn(module_qn, file_path)
        self.module_qn_to_file_path[module_qn] = file_path
        self._for(language).record_module_facts(module_qn, root_node)

        self._modules.write(
            file_path,
            relative_path,
            module_qn,
            language,
            root_node,
            structural_elements,
        )
        return module_qn

    def _ingest_definitions(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None,
    ) -> None:
        # A language whose types own the names its functions share runs
        # its class pass first, and no inline-module pass (Julia's
        # `JuliaFileIngest` says why).
        if self._for(language).types_before_functions:
            self.class_ingest.ingest_classes_and_methods(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures=combined_captures,
            )
            self.function_ingest.ingest_all_functions(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures=combined_captures,
            )
            return
        self.class_ingest.ingest_inline_modules(
            root_node,
            module_qn,
            language,
            queries,
            combined_captures=combined_captures,
        )
        self.function_ingest.ingest_all_functions(
            root_node,
            module_qn,
            language,
            queries,
            combined_captures=combined_captures,
        )
        self.class_ingest.ingest_classes_and_methods(
            root_node,
            module_qn,
            language,
            queries,
            combined_captures=combined_captures,
        )

    def keep_sql_source(
        self, file_path: Path, root_node: ASTNode, parsed: bytes
    ) -> None:
        """Note the bytes a SQL tree was parsed from, when its text is not them
        (`SqlFileIngest.keep_source`)."""
        self._sql.keep_source(file_path, root_node, parsed)

    def _fold_overloads(
        self,
        root_node: ASTNode,
        module_qn: str,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        combined_captures: dict[str, list] | None,
    ) -> None:
        if (
            folded := self._for(language).folded_overloads(
                root_node, language, queries, combined_captures
            )
        ) is not None:
            self.folded_overloads[module_qn] = folded

    def process_dependencies(self, filepath: Path) -> None:
        logger.debug(ls.DEF_PARSING_DEPENDENCY.format(path=filepath))

        manifest = read_manifest(filepath)
        if manifest.unparsable is not None:
            self.unparsable_manifests[filepath] = manifest.unparsable
        for dep in manifest.dependencies:
            self._add_dependency(dep.name, dep.spec, dep.properties)

    def _add_dependency(
        self, dep_name: str, dep_spec: str, properties: dict[str, str] | None = None
    ) -> None:
        if not dep_name or dep_name.lower() in cs.EXCLUDED_DEPENDENCY_NAMES:
            return

        logger.debug(ls.DEF_FOUND_DEPENDENCY.format(name=dep_name, spec=dep_spec))
        self.ingestor.ensure_node_batch(
            cs.NodeLabel.EXTERNAL_PACKAGE, {cs.KEY_NAME: dep_name}
        )

        rel_properties = {cs.KEY_VERSION_SPEC: dep_spec} if dep_spec else {}
        if properties:
            rel_properties |= properties

        self.ingestor.ensure_relationship_batch(
            (cs.NodeLabel.PROJECT, cs.KEY_NAME, self.project_name),
            cs.RelationshipType.DEPENDS_ON_EXTERNAL,
            (cs.NodeLabel.EXTERNAL_PACKAGE, cs.KEY_NAME, dep_name),
            properties=rel_properties,
        )

    def _get_docstring(
        self, node: ASTNode, language: cs.SupportedLanguage
    ) -> str | None:
        """The documentation of one definition, in its language's own form.

        Python's is a string literal inside the body; every other language
        marks it with a comment convention above the declaration, which needs
        the per-language markers in `definition_docstring` because the
        grammars report a doc comment and an ordinary one as the same node
        type (issue #1809). `language` is required rather than defaulted: a
        default would silently route every call site that forgot it back to
        the Python path, and the property would stay empty for that language
        with nothing failing.
        """
        if language != cs.SupportedLanguage.PYTHON:
            return extract_definition_docstring(node, language) or signature_docstring(
                node, language
            )
        return python_docstring(node)
