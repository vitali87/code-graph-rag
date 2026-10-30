"""The definition-level resolution a sync defers until every file of it is
parsed: C/C++ out-of-class methods and forward declarations, containment,
inheritance and the import maps they all resolve through."""

from collections.abc import Callable, Collection

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..config import settings
from ..parsers.document_tier import DocumentTier
from ..parsers.factory import ProcessorFactory
from ..parsers.go import graph_joins as go_joins
from .frontends import FrontendRunner
from .import_closure import ImportClosure
from .rehydration import GraphRehydrator, RehydrationRun


class DeferredResolution:
    """Every deferred definition-level stage that must follow Pass 2.

    Shared by `GraphUpdater.run` and `GraphUpdater.reingest` (issue #1524): a
    scoped re-ingest deletes a file's Module subtree like an incremental run
    does, so the C/C++ macro, containment, forward-declaration, inheritance
    and module-implementation relationships it held have to be rebuilt by
    the same stages, or they vanish until a full update.
    `known_module_paths` names every module qn the run produced.
    """

    def __init__(
        self,
        factory: ProcessorFactory,
        document_tier: DocumentTier,
        frontend_runner: FrontendRunner,
        graph_rehydrator: GraphRehydrator,
        import_closure: ImportClosure,
        known_module_paths: Callable[[], dict[str, str]],
    ) -> None:
        self._factory = factory
        self._document_tier = document_tier
        self._frontend_runner = frontend_runner
        self._graph_rehydrator = graph_rehydrator
        self._import_closure = import_closure
        self._known_module_paths = known_module_paths

    def resolve_deferred_definitions(
        self, run: RehydrationRun, rehydrate: bool, reparsed_keys: Collection[str]
    ) -> dict[str, str]:
        """Run the stages; the module qn -> path map the IMPORTS flush
        verifies against. `rehydrate` reads the registry of the files the run
        did not re-parse, `reparsed_keys`, back from the graph first."""
        # First: every module of the run is named now, and everything below
        # (inheritance, Pass 3) resolves through the import map.
        self._factory.import_processor.point_imports_at_own_language_siblings(
            self._factory.definition_processor.module_qn_to_file_path
        )
        # Every File node of the pass is buffered by now, so a Markdown link
        # to a file parsed after its document finds its target (issue #2400).
        self._document_tier.emit_pending_links()
        # HYBRID must run after Pass 2: an incremental run deletes each
        # changed file's Module subtree before re-parsing it, so macro
        # nodes and include IMPORTS emitted earlier would be deleted with
        # it and vanish until a forced rebuild.
        if settings.CPP_FRONTEND == cs.CppFrontend.HYBRID:
            self._frontend_runner.run_cpp_frontend()

        go_methods = self._factory.definition_processor.resolve_deferred_go_methods()
        if go_methods:
            logger.info(ls.GO_RECEIVER_METHODS_RESOLVED.format(count=go_methods))

        if rehydrate:
            self._graph_rehydrator.rehydrate_registry_from_graph(run, reparsed_keys)

        # After rehydration, which tells a re-export from a definition, and
        # before every stage below that resolves through the import maps.
        self._import_closure.restore_reached()

        self._resolve_cpp_definitions(reparsed_keys)

        # IMPORTS edges and the Rust sub-scope arbitration verify against
        # every module qn this run produced (files, inline modules,
        # rehydrated unchanged files); an internal target that resolves
        # nowhere emits no edge.
        known_module_paths = self._known_module_paths()
        self._resolve_parents(known_module_paths)
        return known_module_paths

    def _resolve_cpp_definitions(self, reparsed_keys: Collection[str]) -> None:
        """The C/C++ definitions Pass 2 could not bind while it parsed."""
        definition_processor = self._factory.definition_processor
        # After rehydration: an incremental run re-parses the `.cpp` holding
        # an out-of-class method while its class stays in an unchanged
        # header, and the class is only known once the registry is read back
        # from the graph. Resolving earlier bound such a method to a
        # module-anchored fallback qn beside its class-anchored node, in a
        # parse-order-dependent way (issue #1552).
        corrected = definition_processor.resolve_deferred_cpp_methods()
        if corrected:
            logger.info(ls.CPP_OUT_OF_CLASS_METHODS_RESOLVED.format(count=corrected))

        contained = definition_processor.resolve_deferred_cpp_containment()
        if contained:
            logger.info(ls.CPP_NESTED_CONTAINMENTS_RESOLVED.format(count=contained))

        # After resolve_deferred_cpp_methods: an out-of-class method's span
        # is recorded only once its class binding resolves, and a macro use
        # inside such a method must attribute to it, not the Module.
        self._frontend_runner.resolve_hybrid_macro_calls(reparsed_keys)

        # After rehydration: an expansion call's callee join needs spans
        # for unchanged files too.
        self._frontend_runner.resolve_hybrid_expansion_calls(reparsed_keys)

        # After rehydration so the "does a real definition exist?" check sees
        # definitions in files an incremental run did not re-parse; otherwise a
        # forward declaration whose definition lives in an unchanged file is
        # kept as a phantom and re-fragments the class.
        kept_forwards = definition_processor.resolve_deferred_forward_declarations()
        if kept_forwards:
            logger.info(ls.CPP_FORWARD_TYPES_REGISTERED.format(count=kept_forwards))

        # After rehydration (an incremental run's class may live in an
        # unchanged header) and after forward declarations (a kept
        # forward-declared TYPE also proves the name is a class, not a
        # macro).
        orphan_ctors = definition_processor.resolve_deferred_cpp_artifacts()
        if orphan_ctors:
            logger.info(ls.CPP_ORPHAN_CTORS_REGISTERED.format(count=orphan_ctors))

        # After artifact resolution so a recovery-registered definition also
        # counts; a prototype duplicating any bodied definition is dropped.
        kept_prototypes = definition_processor.resolve_deferred_cpp_prototypes()
        if kept_prototypes:
            logger.info(ls.CPP_PROTOTYPES_REGISTERED.format(count=kept_prototypes))

        # After forward declarations so a base whose only representation is
        # a kept forward declaration still resolves to a real node.
        inherits = definition_processor.resolve_deferred_cpp_inherits()
        if inherits:
            logger.info(ls.CPP_INHERITANCE_BASES_RESOLVED.format(count=inherits))

    def _resolve_parents(self, known_module_paths: dict[str, str]) -> None:
        """Every language's parents and containers, against the full registry."""
        definition_processor = self._factory.definition_processor
        import_processor = self._factory.import_processor
        # Inline-mod import maps commit BEFORE the deferred inheritance
        # pass below: it re-resolves module-anchored trait guesses through
        # them.
        import_processor.rust.finalise_rust_mod_scope_uses(known_module_paths)
        # Same for Scala: a package import resolves only once every module
        # declaring a package is known, rehydrated ones included (#2450).
        import_processor.scala.resolve_scala_project_imports(known_module_paths)

        # Same reasoning for every other language: parents resolve against
        # the full registry (including rehydrated definitions), and an
        # unresolvable parent emits no edge instead of a phantom.
        generic_inherits = definition_processor.resolve_deferred_inherits()
        if generic_inherits:
            logger.info(ls.INHERITANCE_PARENTS_RESOLVED.format(count=generic_inherits))

        # Every Go type and method is registered by now, rehydrated ones
        # included, so method sets are complete (issue #2483).
        go_joins.link_interfaces(definition_processor)

        module_impls = definition_processor.resolve_deferred_cpp_module_impls()
        if module_impls:
            logger.info(ls.CPP_MODULE_IMPLS_RESOLVED.format(count=module_impls))

        # Last containment step: every node-registering pass above (deferred
        # C++ methods, Go receivers, kept forward declarations) must finish
        # before parent qns are verified against the registry.
        linked = definition_processor.resolve_deferred_parent_links()
        if linked:
            logger.info(ls.CONTAINMENT_PARENTS_RESOLVED.format(count=linked))
        self._graph_rehydrator.ungroup_rehydrated_non_partials()
        # Return/parameter annotations resolve against the complete registry
        # (issue #1527), so a type defined in a later file still gets its edge.
        typed = definition_processor.emit_type_edges()
        if typed:
            logger.info(ls.TYPE_EDGES_EMITTED, count=typed)
