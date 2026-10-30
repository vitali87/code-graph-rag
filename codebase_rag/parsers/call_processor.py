"""The call pass: the CALLS, REFERENCES and INSTANTIATES edges of every file.

`CallProcessor` drives the pass and keeps for itself the order the steps run
in: per file, the callers its `CallerScan` names (`call_callers`, sharing
out the file's calls through `call_ownership`) and the module's own share of
the calls, each scanned by its `CallerCalls` (`call_scan`): the caller's
local types, the side passes that read it (I/O, RPC exposure, data flow),
the callable parameters it records, the references its language writes, and
the `CallScan` every one of its call nodes is bound with by the
`CallBinding`. It owns the edge site and the resolution each edge carries,
and writes every edge (`emit_rel`). A language's share of the pass is the
`LanguageCalls` strategy its package builds, looked up by `calls_for`; what
languages share is in the collaborators beside this module: the reference
walkers (`call_references`, and `call_tables` for the functions a literal
holds), the callback edges they write through (`call_callbacks`),
callable-parameter flow (`callable_flow`) and the construction a class
callee runs (`call_construction`)."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..capture import ALL_ENABLED, CaptureSelection
from ..node_text import decode_node_text
from ..services import IngestorProtocol
from ..types_defs import (
    ASTCacheProtocol,
    FunctionLocation,
    FunctionRegistryTrieProtocol,
    LanguageQueries,
    PropertyDict,
)
from ..utils import qn_markers
from ..utils.path_utils import cached_relative_path
from .call_binding import CallBinding
from .call_callbacks import CallbackEdges, verdict_of
from .call_callers import CallerScan, FileCalls
from .call_construction import CallConstruction
from .call_nodes import (
    call_site_properties,
    call_target_name,
    function_field_call_name,
)
from .call_ownership import (
    collect_all_call_nodes,
    file_call_nodes,
    file_combined_captures,
)
from .call_references import ReferenceWalkers
from .call_resolver import CallResolver, Resolved
from .call_scan import CallerCalls
from .call_tables import TableReferences
from .callable_flow import CallableFlow
from .cpp import calls as cpp_calls
from .csharp import calls as csharp_calls
from .dart import calls as dart_calls
from .definition_facts import DefinitionFacts
from .dispatch_registry import DispatchRegistryProcessor
from .flow_access import FlowProcessor
from .go import calls as go_calls
from .go.link_state import GoLinkState
from .import_processor import ImportProcessor
from .io_access import IOAccessProcessor
from .io_access.processor import PackageFileLookups
from .java import calls as java_calls
from .js_ts import calls as js_calls
from .julia import calls as julia_calls
from .language_calls import LanguageCalls
from .lua import calls as lua_calls
from .php import calls as php_calls
from .python import calls as python_calls
from .rpc_exposure import GoRpcExposureProcessor
from .rust import calls as rs_calls
from .scala import calls as scala_calls
from .sql import calls as sql_calls
from .string_call import load_string_call_specs
from .type_inference import TypeInferenceEngine
from .utils import function_span_key, shared_or_new

_RESOLVED_RELS = frozenset(
    {
        cs.RelationshipType.CALLS,
        cs.RelationshipType.REFERENCES,
        cs.RelationshipType.INSTANTIATES,
    }
)


class CallProcessor:
    __slots__ = (
        "ingestor",
        "note_unresolved",
        "site_node",
        "resolution",
        "verdict",
        "_site_cache",
        "repo_path",
        "project_name",
        "module_qn_to_file_path",
        "_path_to_module_qn",
        "facts",
        "_resolver",
        "_go",
        "_cpp",
        "_dart",
        "_csharp",
        "_js",
        "_python",
        "_java",
        "_scala",
        "_php",
        "_c",
        "_sql",
        "_rust",
        "_lua",
        "_julia",
        "_language_calls",
        "_default_calls",
        "callbacks",
        "tables",
        "references",
        "_flow",
        "construction",
        "binding",
        "callers",
        "scan",
        "failed_call_files",
        "_call_file",
        "_flow_processor",
        "_dispatch_registry",
    )

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: FunctionRegistryTrieProtocol,
        import_processor: ImportProcessor,
        type_inference: TypeInferenceEngine,
        facts: DefinitionFacts,
        capture: CaptureSelection | None = None,
        module_qn_to_file_path: dict[str, Path] | None = None,
        ast_cache: ASTCacheProtocol | None = None,
        go_links: GoLinkState | None = None,
    ) -> None:
        self.ingestor = ingestor
        # The tree-sitter node whose span every edge emitted right now records
        # as its site (issue #1522); each pass sets it per visited node and
        # `host_site_scoped` restores it on exit. The cache keeps the variant
        # fan-out of one site from re-deriving the same property dict.
        self.site_node: Node | None = None
        # Confidence of the edges emitted next (issue #1526); the main call
        # loop sets it from the resolver, fan-outs mark overloads.
        self.resolution: str = cs.EdgeResolution.EXACT
        # How surely the call node being scanned binds its callee (issue
        # #1526): noted where the scan resolves it, adjusted by the judgments
        # that follow, and read when its edges are emitted. The passes over
        # its arguments resolve their own references and keep their own.
        self.verdict: str = cs.EdgeResolution.EXACT
        # Keyed by the node OBJECT, not Node.id: tree-sitter recycles ids
        # across trees, and holding the node keeps its id from being reused.
        self._site_cache: tuple[Node, PropertyDict] | None = None
        self.repo_path = repo_path
        self.project_name = project_name
        self.module_qn_to_file_path = shared_or_new(module_qn_to_file_path, dict)
        self._path_to_module_qn: dict[Path, str] | None = None
        self.facts = facts

        # The import processor owns the record (#1568); every pass notes
        # through it so the empty-name guard lives in one place.
        self.note_unresolved = import_processor.note_unresolved
        self._resolver = CallResolver(
            function_registry=function_registry,
            import_processor=import_processor,
            type_inference=type_inference,
            facts=self.facts,
            go_links=go_links,
        )
        self._go = go_calls.GoCallResolution(
            self, self._resolver, self.module_qn_to_file_path, self.facts
        )
        self._cpp = cpp_calls.CppCallResolution(self, self._resolver, self.facts)
        self._dart = dart_calls.DartCallResolution(self, self._resolver)
        self._csharp = csharp_calls.CSharpCallResolution(
            self, self._resolver, project_name, self.module_qn_to_file_path
        )
        self._js = js_calls.JsTsCallResolution(self, self._resolver)
        self._python = python_calls.PythonCallResolution(self, self._resolver)
        self._java = java_calls.JavaCallResolution(self, self._resolver)
        self._scala = scala_calls.ScalaCallResolution(self, self._resolver)
        self._php = php_calls.PhpCallResolution(self, self._resolver)
        self._c = cpp_calls.CCallResolution(
            self, self._resolver, self.module_qn_to_file_path
        )
        self._sql = sql_calls.SqlCallResolution(self, self.facts)
        self._rust = rs_calls.RustCallResolution(self._resolver)
        self._lua = lua_calls.LuaCallResolution()
        self._julia = julia_calls.JuliaCallResolution(self)
        self._language_calls = self._language_call_lookup()
        self._default_calls = LanguageCalls()
        # What every language shares: the edge a function handed over as a
        # value keeps alive, the walks that find such values, callable-
        # parameter flow, a class callee's construction, the steps a call
        # node goes through, and the callers a file's calls are shared out to.
        # The language collaborators above call the public ones directly
        # (`call_host`): built after them, they are reached through this
        # processor when a pass runs rather than handed over at construction.
        self.callbacks = CallbackEdges(self, self._resolver.function_registry)
        self.tables = TableReferences(self, self.callbacks, self._resolver, self._js)
        self.references = ReferenceWalkers(
            self, self.callbacks, self.tables, self._resolver, self._js
        )
        self._flow = CallableFlow(self, self.callbacks, self._resolver)
        self.construction = CallConstruction(self, self._resolver, self.facts)
        self.binding = CallBinding(
            self,
            self._resolver,
            self.facts,
            self.callbacks,
            self._flow,
            self.construction,
            # Dispatchers that name their callee in a string argument,
            # declared in the repository's .cgr.toml.
            load_string_call_specs(repo_path),
        )
        # Files whose call pass failed this run, whole or in one caller, for
        # the sync summary (issue #3173); `_call_file` is the file being walked.
        self.failed_call_files: set[Path] = set()
        self._call_file: Path | None = None
        selection = capture if capture is not None else ALL_ENABLED
        io_access = IOAccessProcessor(
            ingestor,
            import_processor,
            selection=selection,
            package_files=PackageFileLookups(
                module_paths=self.module_qn_to_file_path,
                ast_cache=ast_cache,
                go_package_names=self.facts.go_package_names,
            ),
            project_name=project_name,
        )
        self._dispatch_registry = DispatchRegistryProcessor(
            ingestor=ingestor,
            selection=selection,
            function_registry=function_registry,
        )
        rpc_exposure = GoRpcExposureProcessor(
            ingestor=ingestor,
            import_processor=import_processor,
            selection=selection,
            function_registry=function_registry,
            simple_name_lookup=type_inference.simple_name_lookup,
            module_paths=self.module_qn_to_file_path,
            go_package_names=self.facts.go_package_names,
            go_function_return_types=type_inference.facts.go_function_return_types,
            ast_cache=ast_cache,
        )
        self._flow_processor = FlowProcessor(
            ingestor,
            import_processor,
            self._resolver,
            selection=selection,
            function_locations=self.facts.function_locations,
            project_name=project_name,
        )
        self.scan = CallerCalls(
            self,
            self._resolver,
            self._flow,
            self.binding,
            io_access=io_access,
            rpc_exposure=rpc_exposure,
            flow_access=self._flow_processor,
        )
        self.callers = CallerScan(self, self._julia, self.scan)

    def _language_call_lookup(self) -> dict[str, LanguageCalls]:
        # The call strategy each language's files go to; JS, TS and TSX share
        # one. A language with none, and a walk that names no language, get
        # the base, which adds nothing to the pass.
        return {
            cs.SupportedLanguage.PYTHON: self._python,
            cs.SupportedLanguage.JS: self._js,
            cs.SupportedLanguage.TS: self._js,
            cs.SupportedLanguage.TSX: self._js,
            cs.SupportedLanguage.JAVA: self._java,
            cs.SupportedLanguage.GO: self._go,
            cs.SupportedLanguage.CPP: self._cpp,
            cs.SupportedLanguage.C: self._c,
            cs.SupportedLanguage.PHP: self._php,
            cs.SupportedLanguage.CSHARP: self._csharp,
            cs.SupportedLanguage.DART: self._dart,
            cs.SupportedLanguage.SCALA: self._scala,
            cs.SupportedLanguage.SQL: self._sql,
            cs.SupportedLanguage.RUST: self._rust,
            cs.SupportedLanguage.LUA: self._lua,
            cs.SupportedLanguage.JULIA: self._julia,
        }

    def calls_for(self, language: str | None) -> LanguageCalls:
        if language is None:
            return self._default_calls
        return self._language_calls.get(language, self._default_calls)

    def emit_rel(
        self,
        from_spec: tuple[str, str, str],
        rel_type: str,
        to_spec: tuple[str, str, str],
        properties: PropertyDict | None = None,
    ) -> None:
        # Every CALLS/REFERENCES/INSTANTIATES edge this processor emits goes
        # through here so the current site (line/col/arg shape of the
        # producing expression) rides along as edge properties (#1522).
        # Invoking a class constructs it. A direct `Config()` is written as
        # INSTANTIATES, and a class reached through a callable parameter
        # (`ensure(Config)` running `obj_type()`) is the same fact; as CALLS it
        # was an edge type the schema does not have (issue #2394).
        if rel_type == cs.RelationshipType.CALLS and to_spec[0] == cs.NodeLabel.CLASS:
            rel_type = cs.RelationshipType.INSTANTIATES
        site = self.site_node
        if site is not None:
            cached = self._site_cache
            if cached is None or cached[0] is not site:
                cached = (site, call_site_properties(site))
                self._site_cache = cached
            properties = {**cached[1], **properties} if properties else dict(cached[1])
        if rel_type in _RESOLVED_RELS:
            properties = {**(properties or {}), cs.KEY_RESOLUTION: self.resolution}
        if properties:
            self.ingestor.ensure_relationship_batch(
                from_spec, rel_type, to_spec, properties=properties
            )
        else:
            self.ingestor.ensure_relationship_batch(from_spec, rel_type, to_spec)

    def get_node_name(self, node: Node, field: str = cs.FIELD_NAME) -> str | None:
        name_node = node.child_by_field_name(field)
        if not name_node:
            return None
        text = name_node.text
        # Replacement decode, not strict: one undecodable byte in this file
        # must not abort the whole call pass and strand the file's CALLS
        # edges (issue #1797). This is NOT the call pass's only name funnel
        # -- `_get_call_target_name` decodes call targets separately and is
        # routed through the same helper.
        return None if text is None else decode_node_text(text)

    def _module_qn(self, file_path: Path, relative_path: Path) -> str:
        # The definition pass is the single source of truth for module qns:
        # same-stem siblings (Aggregator.cpp / Aggregator.h) get a
        # collision-disambiguated qn there, and recomputing from the path
        # here attributed every caller inside such a header to a module qn
        # with no node, silently dropping its CALLS (issue #652).
        if self._path_to_module_qn is None:
            self._path_to_module_qn = {
                path: qn for qn, path in self.module_qn_to_file_path.items()
            }
        if registered := self._path_to_module_qn.get(file_path):
            return registered
        file_name = file_path.name
        if file_name in (*cs.PY_PACKAGE_INIT_FILES, cs.MOD_RS):
            return cs.SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.parent.parts)
            )
        return cs.SEPARATOR_DOT.join(
            [self.project_name] + list(relative_path.with_suffix("").parts)
        )

    def collect_callable_field_bindings(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        # Pre-pass: record which functions are bound to a class's callable
        # fields (FQNSpec(get_name=_python_get_name, ...)). Runs before call
        # resolution so a field invocation resolves regardless of which file
        # the construction site lives in. Bindings are recorded PENDING
        # (keyword name or positional index) and resolved by
        # finalize_field_bindings after every file's ctor metadata (param
        # order + param->attribute renames) is collected.
        calls = self.calls_for(language)
        if not calls.binds_callable_fields:
            return
        try:
            module_qn = self._module_qn(
                file_path, cached_relative_path(file_path, self.repo_path)
            )
            call_nodes = (
                func_class_captures_cache[file_path].get(cs.CAPTURE_CALL)
                if func_class_captures_cache is not None
                and file_path in func_class_captures_cache
                else None
            )
            if call_nodes is None:
                call_nodes, _ = collect_all_call_nodes(root_node, language, queries)
            calls.collect_callable_field_bindings(root_node, module_qn, call_nodes)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    def finalize_callable_field_bindings(self) -> None:
        self._resolver.finalize_field_bindings()

    def reset_resolution_caches(self) -> None:
        self._resolver.reset_resolution_caches()
        self._resolver.type_inference.reset_semantic_join_memos()
        # Both indexes are derived from module_qn_to_file_path and rebuilt
        # lazily: the Go package index only when the map's SIZE changes, the
        # path index never. A deleted module removed from the map and a new
        # one added in the same run leave the size unchanged, so a stale
        # entry would survive into Pass 3 and raise on lookup.
        self._go.reset_package_index()
        self._path_to_module_qn = None
        self._c.reset_file_scopes()
        # A re-parsed factory may construct something else now.
        self._js.reset_factory_classes()
        # The C# per-class member index (issue #2453) is read off the
        # registry, which a re-parse just changed.
        if (csharp := self._resolver.type_inference._csharp_type_inference) is not None:
            csharp.reset_member_index()

    def process_calls_in_file(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
        queries: Mapping[cs.SupportedLanguage, LanguageQueries],
        func_class_captures_cache: dict[Path, dict] | None = None,
    ) -> None:
        relative_path = cached_relative_path(file_path, self.repo_path)
        logger.debug(ls.CALL_PROCESSING_FILE, path=relative_path)
        self._flow.begin_file(file_path)
        self._call_file = file_path

        module_qn: str | None = None
        try:
            module_qn = self._module_qn(file_path, relative_path)

            self._dispatch_registry.process_file(root_node, module_qn, language)

            call_name_cache: dict[int, str | None] = {}

            combined_captures = file_combined_captures(
                file_path, root_node, queries[language], func_class_captures_cache
            )

            all_call_nodes, call_starts, func_node_starts = file_call_nodes(
                combined_captures, root_node, language, queries
            )
            sorted_func_nodes = combined_captures.get(cs.CAPTURE_FUNCTION)
            file_calls = FileCalls(
                combined_captures,
                all_call_nodes,
                call_starts,
                func_node_starts,
                call_name_cache,
            )

            self.callers.ingest_function_callers(
                root_node, module_qn, language, queries, file_calls
            )
            # Bare decorators (`@task`) are not call nodes; emit their
            # module-load CALLS before the empty-`all_call_nodes` early return,
            # since a file may have decorators but no other calls. Classes can
            # be decorated too, so include captured class nodes.
            # A dispatch table (HANDLERS = {"k": fn}) at module scope keeps its
            # entries reachable; scan before the no-calls early return so a file
            # that only defines functions and a table is still covered. Runs for
            # every flow language with an object/array/dict literal form.
            calls = self.calls_for(language)
            calls.ingest_module_references(
                root_node,
                module_qn,
                language,
                queries,
                combined_captures,
                sorted_func_nodes,
            )
            if not all_call_nodes and not calls.scans_callers_without_calls:
                # A file with no call expressions has nothing further to
                # process, unless its language's callers make edges without
                # one; those passes run per caller inside class processing,
                # so they proceed.
                return
            self.callers.ingest_class_callers(
                root_node, module_qn, language, queries, file_calls
            )
            module_calls = self.callers.module_level_calls(
                all_call_nodes, call_starts, sorted_func_nodes, language, module_qn
            )
            self.scan.ingest(
                root_node,
                module_qn,
                cs.NodeLabel.MODULE,
                module_qn,
                language,
                queries,
                call_nodes=module_calls,
                call_name_cache=call_name_cache,
            )

        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)
        finally:
            self._call_file = None
            # The receiver binding index is only ever consulted by sites in
            # the SAME file; evicting here (errors and early returns
            # included) keeps the map from retaining one parse tree per
            # JS/TS module for the rest of the pass.
            if module_qn is not None:
                self._js.forget_file_bindings(module_qn)

    def recorded_caller(
        self, func_node: Node, module_qn: str
    ) -> FunctionLocation | None:
        # The registry membership check guards incremental runs, where an
        # unchanged file's locations were not re-recorded this run.
        loc = self.facts.function_locations.get(function_span_key(module_qn, func_node))
        if loc is None or loc.qualified_name not in self._resolver.function_registry:
            return None
        return loc

    def record_caller_failure(
        self, message: str, caller: str, error: Exception
    ) -> None:
        path = self._call_file
        rel_path = None
        if path is not None:
            self.failed_call_files.add(path)
            # POSIX on every OS, like the other paths the logs name.
            rel_path = cached_relative_path(path, self.repo_path).as_posix()
        logger.error(message, caller=caller, path=rel_path, error=error)

    def noted[R: tuple[str, str] | None](self, resolved: R) -> R:
        """`resolved`, with its verdict noted as the call node's."""
        self.verdict = verdict_of(resolved)
        return resolved

    def _resolve_noting(self, *args: Any, **kwargs: Any) -> Resolved | None:
        """The resolver's answer for the call node being scanned, its verdict
        noted for the edge the scan emits."""
        return self.noted(self._resolver.resolve_function_call(*args, **kwargs))

    def _link_sql_routine(
        self,
        module_qn: str,
        caller_spec: tuple[str, str, str],
        call_name: str,
        site: PropertyDict | None = None,
    ) -> None:
        # A SQL routine call has no receiver, import or type to resolve
        # through. The generic resolver reads `billing.fee` as a receiver and
        # a method, drops the receiver and binds `fee` by bare name, so a
        # schema-qualified call landed on whichever schema's `fee` came first.
        #
        # The name is recorded as a waiter even when it resolves: with no
        # imports its target set stays open, and a routine of that name added
        # in ANOTHER file (a second schema for `fee`, a second `billing.fee`
        # overload) is one more target a clean index links. No edge leads from
        # that file to this caller, so only the waiter list (issue #1568) can
        # send it back for a re-parse. The whole normalized name is recorded:
        # `billing.fee` waits on that routine only, never on `audit.fee`.
        self.note_unresolved(module_qn, call_name)
        targets = self._resolver.sql_routine_targets(call_name)
        if not targets:
            # A builtin (`count`, `now`) or a routine no indexed file defines.
            return
        if len({qn_markers.natural_qn(qn) for qn in targets}) > 1:
            # Several routines answer to the name: other schemas for an
            # unqualified one, other files defining a qualified one.
            self.resolution = cs.EdgeResolution.HEURISTIC
        elif len(targets) > 1:
            # One routine with overloads the call site's types would choose.
            self.resolution = cs.EdgeResolution.OVERLOAD
        for target_qn in targets:
            self.emit_rel(
                caller_spec,
                cs.RelationshipType.CALLS,
                (cs.NodeLabel.FUNCTION, cs.KEY_QUALIFIED_NAME, target_qn),
                site,
            )

    def finalize_dispatch(self) -> None:
        self._dispatch_registry.finalize()

    def finalize_flow(self) -> None:
        # Resolve deferred FLOWS_TO return-taint once every function body has
        # been walked, so a callee processed after its caller still contributes
        # its return edge and resource flow (issue #712), and compose forward
        # parameter-taint the same way (issue #1142).
        self._flow_processor.finalize()

    def finalize_callable_param_flow(self) -> None:
        self._flow.finalize()

    def forget_callable_flow(self, file_path: Path) -> None:
        # A deleted file is never re-walked, so nothing else replaces its
        # records; left in place they keep emitting its call sites' edges.
        self._flow.forget_file(file_path)

    def begin_run(self) -> None:
        # A run that walks no calls (in sync) must not report a previous
        # run's failures as its own.
        self.failed_call_files.clear()

    def reset_callable_flow(self) -> None:
        # For a pass that re-walks every file: records of a file deleted
        # since the last pass would otherwise outlive it.
        self._flow.reset()

    def reset_js_receiver_bindings(self) -> None:
        # Despite the historical name this clears ALL per-run JS call-pass
        # state: the receiver-binding index, the symbol index, and the
        # prototype-evidence memo. A reused updater's next pass re-parses
        # files and must rebuild each from the fresh trees.
        self._js.reset_run_state()

    def collect_js_symbol_bindings(
        self,
        file_path: Path,
        root_node: Node,
        language: cs.SupportedLanguage,
    ) -> None:
        # Pre-pass for symbol-keyed member dispatch (issue #989), one walk
        # per JS/TS file.
        calls = self.calls_for(language)
        if not calls.keys_members_by_symbol:
            return
        try:
            module_qn = self._module_qn(
                file_path, cached_relative_path(file_path, self.repo_path)
            )
        except ValueError:
            return
        try:
            calls.collect_symbol_bindings(root_node, module_qn)
        except Exception as e:  # noqa: BLE001 -- isolation: one unit's failure must not end the run
            self.failed_call_files.add(file_path)
            logger.error(ls.CALL_PROCESSING_FAILED, path=file_path, error=e)

    def finalize_js_symbol_bindings(self) -> None:
        self._js.finalize_symbol_bindings()

    @property
    def js_symbol_constants(self) -> set[str]:
        return self._js.symbol_constants

    @property
    def js_symbol_member_types(self) -> dict[str, set[str]]:
        return self._js.symbol_member_types

    # `call_nodes` reads Go's and JS/TS's call syntax, so neither language's
    # module can import it; the pass, which imports both, names a call for
    # their collaborators.

    def _get_call_target_name(
        self, call_node: Node, language: cs.SupportedLanguage | None = None
    ) -> str | None:
        return call_target_name(call_node, self.calls_for(language))

    @staticmethod
    def _function_field_call_name(func_child: Node, calls: LanguageCalls) -> str | None:
        return function_field_call_name(func_child, calls)
