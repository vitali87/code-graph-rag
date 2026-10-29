"""Emit the HTTP endpoints a project's routes expose, and link client URLs and
contract files to them."""

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from loguru import logger
from tree_sitter import Node

from .. import constants as cs
from .. import logs as ls
from ..capture import CaptureSelection
from ..graph_query import opt_int
from ..path_filters import matches_test_path
from ..services import IngestorProtocol, QueryingIngestorProtocol, QueryProtocol
from ..types_defs import ASTCacheProtocol, FunctionSpanKey, PropertyParams
from .contract_linking import link_contracts
from .endpoint_prefixes import (
    CYPHER_DELETE_HANDLER_EXPOSES,
    CYPHER_PROJECT_PY_MODULES,
    CYPHER_PROJECT_ROUTE_HANDLERS,
    build_router_registry,
    imported_module_qns,
)
from .endpoint_routes import (
    CYPHER_DELETE_MODULE_EXPOSES,
    CYPHER_MODULE_FUNCTION_STARTS,
    CYPHER_PROJECT_MODULES,
    ROUTE_CALL_LANGUAGES,
    ROUTE_MODULE_EXTENSIONS,
    RouteRegistration,
    collect_route_registrations,
)
from .endpoints import (
    emit_endpoint,
    emit_endpoints,
    link_endpoints,
    parse_route_decorator,
)
from .factory import ProcessorFactory


class EndpointRun(Protocol):
    """What the endpoint passes read and set on the sync run they are part
    of: whether it re-parses every file, whether the project registry could
    be read, and whether an EXPOSES cleanup was skipped and is owed."""

    is_full_build: bool
    registry_unread: bool
    exposes_cleanup_skipped: bool


def _owning_module_qn(qn: str, module_qns: set[str]) -> str | None:
    owner: str | None = None
    for candidate in module_qns:
        if qn.startswith(candidate + cs.SEPARATOR_DOT) and (
            owner is None or len(candidate) > len(owner)
        ):
            owner = candidate
    return owner


def _route_label(node_type: cs.NodeLabel) -> cs.NodeLabel:
    return (
        cs.NodeLabel.METHOD
        if node_type == cs.NodeLabel.METHOD
        else cs.NodeLabel.FUNCTION
    )


def _route_handler_entry(
    row: Mapping[str, object], already_pending: set[str], module_qns: set[str]
) -> tuple[cs.NodeLabel, str, list[str], str | None] | None:
    qn = row.get(cs.KEY_QUALIFIED_NAME)
    decorators = row.get(cs.KEY_DECORATORS)
    if not isinstance(qn, str) or qn in already_pending:
        return None
    if not isinstance(decorators, list):
        return None
    routes = [d for d in decorators if isinstance(d, str) and parse_route_decorator(d)]
    if not routes:
        return None
    labels = row.get("labels")
    label = (
        cs.NodeLabel.METHOD
        if isinstance(labels, list) and cs.NodeLabel.METHOD.value in labels
        else cs.NodeLabel.FUNCTION
    )
    return (label, qn, routes, _owning_module_qn(qn, module_qns))


def _package_modules(module_files: Mapping[str, Path]) -> frozenset[str]:
    # The module qns that are a package's `__init__.py`: a relative import
    # there resolves against the package itself (issue #3192).
    return frozenset(
        qn for qn, path in module_files.items() if path.name in cs.PY_PACKAGE_INIT_FILES
    )


class EndpointLinker:
    """Endpoint nodes for a project's routes, and the links into them.

    Decorated handlers are queued by the definition pass and emitted once
    every module is parsed, so a router mount prefix can resolve across
    modules; call-registered routes (Express, net/http, echo, gin) are read
    from each route-capable module's AST. `owns` says whether this project,
    and not one nested under it, owns a qualified name the graph returned.
    """

    def __init__(
        self,
        ingestor: IngestorProtocol,
        graph: QueryProtocol | None,
        sink: IngestorProtocol,
        capture: CaptureSelection,
        factory: ProcessorFactory,
        ast_cache: ASTCacheProtocol,
        repo_path: Path,
        project_name: str,
        owns: Callable[[str], bool],
    ) -> None:
        # The raw store, which the link passes query and flush; the same
        # store as a readable graph, None for a write-only sink; and the
        # capture-filtered view every endpoint is emitted through.
        self._ingestor = ingestor
        self._graph = graph
        self._sink = sink
        self._capture = capture
        self._factory = factory
        self._ast_cache = ast_cache
        self._repo_path = repo_path
        self._project_name = project_name
        self._owns = owns

    def emit_pending_endpoints(
        self, run: EndpointRun, only: set[str] | None = None
    ) -> None:
        # `only` (a scoped re-ingest, issue #1524) limits the AST loads to
        # those modules plus the router modules their mounts need; the
        # mount-prefix registry is still built from every Python module the
        # graph knows, but through the disk-backed cache only for the
        # scoped set.
        if not self._capture.rel_enabled(cs.RelationshipType.EXPOSES):
            return
        dp = self._factory.definition_processor
        known_files = self._python_module_files()
        if only is None:
            module_files = known_files
            module_asts = self._python_module_asts(module_files)
        else:
            # The router modules a mounting module's mounts need are the
            # ones it imports, transitively (a mount chain main -> api ->
            # routes needs all three); they load through the cache, and
            # their handlers rehydrate below so a mount-prefix edit
            # re-emits them.
            module_files, module_asts = self._scoped_module_closure(only, known_files)
        entries = list(dp.pending_endpoints)
        # A mount-only incremental change re-parses just the mounting module;
        # the unchanged handlers must still re-emit under the new prefix, so
        # they come back from the graph. A full build queued them all already.
        if not run.is_full_build:
            entries.extend(
                self._rehydrated_endpoint_entries(
                    entries, only, known_files, module_asts
                )
            )
        if not entries:
            return
        self._drop_stale_handler_exposes(
            run, [qn for _label, qn, _decorators, _module in entries]
        )
        registry = build_router_registry(
            module_asts, _package_modules({**known_files, **module_files})
        )
        for label, qn, decorators, module_qn in entries:
            # Test modules stay in the stale-EXPOSES drop above (so a
            # legacy graph sheds their endpoints) but emit nothing: a route
            # spun up inside a test is not a production endpoint (#910).
            # Rehydrated handlers live in graph-backed modules absent from
            # the re-parse map, so the merged python-files map goes first.
            if self._is_test_module(
                module_files.get(module_qn or "")
                or dp.module_qn_to_file_path.get(module_qn or "")
            ):
                continue
            emit_endpoints(
                self._sink,
                label,
                qn,
                decorators,
                module_qn=module_qn,
                prefix_resolver=registry.mount_prefixes,
            )
        dp.pending_endpoints.clear()

    def _scoped_module_closure(
        self, only: set[str], known: Mapping[str, Path]
    ) -> tuple[dict[str, Path], dict[str, Node]]:
        # `only` plus every known module reachable through their imports,
        # resolved from the ASTs the way the router registry resolves them.
        files: dict[str, Path] = {}
        asts: dict[str, Node] = {}
        pending = [qn for qn in only if qn in known]
        module_qns = set(known)
        while pending:
            qn = pending.pop()
            if qn in files:
                continue
            files[qn] = known[qn]
            loaded = self._python_module_asts({qn: known[qn]})
            if qn not in loaded:
                continue
            asts[qn] = loaded[qn]
            pending.extend(
                target
                for target in imported_module_qns(
                    qn,
                    loaded[qn],
                    module_qns,
                    is_package=known[qn].name in cs.PY_PACKAGE_INIT_FILES,
                )
                if target not in files
            )
        return files, asts

    def _drop_stale_handler_exposes(
        self, run: EndpointRun, handler_qns: list[str]
    ) -> None:
        # Re-emitted handlers drop their previous EXPOSES first, so a
        # template that changed prefix loses its stale anchor and the orphan
        # cleanup prunes the outdated endpoint node.
        self._drop_stale_exposes(
            run, CYPHER_DELETE_HANDLER_EXPOSES, {"qns": handler_qns}
        )

    def _drop_stale_exposes(
        self, run: EndpointRun, query: str, params: PropertyParams
    ) -> None:
        graph = self._graph
        if graph is None:
            return
        # With the registry unread, ownership degrades to the prefix rule and
        # `svc.v2`'s handlers can pass as `svc`'s (a seeded module map can hold
        # a sibling project's modules the same way), so no delete runs at all;
        # emission only MERGEs, and the next healthy run cleans up
        # (PR #2129).
        if run.registry_unread:
            run.exposes_cleanup_skipped = True
            return
        try:
            graph.execute_write(query, params)
        except Exception:  # noqa: BLE001 -- any engine's driver error; the write is retried later
            # A delete that failed is owed exactly like one that was skipped.
            run.exposes_cleanup_skipped = True
            logger.debug(ls.EXPOSES_CLEANUP_UNAVAILABLE)

    def emit_route_call_endpoints(
        self, run: EndpointRun, only: set[str] | None = None
    ) -> None:
        if not self._capture.rel_enabled(cs.RelationshipType.EXPOSES):
            return
        modules = self._route_module_asts(run, only=only)
        if not modules:
            return
        # Cleanup keyed on every scanned module, BEFORE emission:
        # a module whose last route disappeared (or whose attribution moved)
        # still sheds its old EXPOSES edges even though it contributes no new
        # registrations.
        self._drop_stale_module_exposes(run, sorted(modules))
        registrations = {
            module_qn: collect_route_registrations(root, language)
            for module_qn, (root, language, path) in modules.items()
            # Kept in the cleanup above, skipped for emission (#910).
            if not self._is_test_module(path)
        }
        inline = self._inline_route_handlers(registrations)
        for module_qn, module_registrations in registrations.items():
            for registration in module_registrations:
                label, source_qn = self._route_source(module_qn, registration, inline)
                identity = f"{registration.method} {registration.path}"
                emit_endpoint(self._sink, label, source_qn, identity)

    def _inline_route_handlers(
        self, registrations: Mapping[str, list[RouteRegistration]]
    ) -> dict[FunctionSpanKey, tuple[cs.NodeLabel, str]]:
        # The node of every inline handler, keyed by where the function
        # starts (issue #2521): recorded by this run's definition pass, else
        # read back from the graph for a module this run did not parse. A
        # re-parsed module's old subtree is deleted before its parse, so the
        # graph never answers for it with a previous parse's node.
        found: dict[FunctionSpanKey, tuple[cs.NodeLabel, str]] = {}
        unrecorded: set[str] = set()
        for module_qn, module_registrations in registrations.items():
            for registration in module_registrations:
                if registration.handler_start is None:
                    continue
                row, col = registration.handler_start
                key = (module_qn, row + 1, col)
                if (source := self._recorded_function_at(key)) is not None:
                    found[key] = source
                else:
                    unrecorded.add(module_qn)
        if unrecorded:
            for key, source in self._graph_function_starts(unrecorded).items():
                found.setdefault(key, source)
        return found

    def _recorded_function_at(
        self, key: FunctionSpanKey
    ) -> tuple[cs.NodeLabel, str] | None:
        # The function this run's definition pass recorded starting at `key`.
        dp = self._factory.definition_processor
        location = dp.facts.function_locations.get(key)
        if location is None:
            return None
        node_type = dp.function_registry.get(location.qualified_name)
        if node_type is None:
            return None
        return _route_label(node_type), location.qualified_name

    def _graph_function_starts(
        self, module_qns: set[str]
    ) -> dict[FunctionSpanKey, tuple[cs.NodeLabel, str]]:
        graph = self._graph
        if graph is None:
            return {}
        try:
            rows = graph.fetch_all(
                CYPHER_MODULE_FUNCTION_STARTS, {"module_qns": sorted(module_qns)}
            )
        except Exception:  # noqa: BLE001 -- any engine's driver error; the read degrades
            # The next rung of the ladder still anchors the endpoint.
            logger.debug(ls.ROUTE_HANDLER_STARTS_UNREADABLE)
            return {}
        out: dict[FunctionSpanKey, tuple[cs.NodeLabel, str]] = {}
        for row in rows:
            module_qn = row.get(cs.KEY_MODULE_QN)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            line = opt_int(row.get(cs.KEY_START_LINE))
            col = opt_int(row.get(cs.KEY_START_COL))
            if not (
                isinstance(module_qn, str)
                and isinstance(qn, str)
                and line is not None
                and col is not None
            ):
                continue
            label = (
                cs.NodeLabel.METHOD
                if row.get(cs.KEY_LABEL) == cs.NodeLabel.METHOD.value
                else cs.NodeLabel.FUNCTION
            )
            out[(module_qn, line, col)] = (label, qn)
        return out

    def _drop_stale_module_exposes(
        self, run: EndpointRun, module_qns: list[str]
    ) -> None:
        # Ownership is the DEFINES containment closure from each Module
        # node, so prefix-sharing sibling modules keep their endpoints.
        self._drop_stale_exposes(
            run, CYPHER_DELETE_MODULE_EXPOSES, {"module_qns": module_qns}
        )

    def _route_source(
        self,
        module_qn: str,
        registration: RouteRegistration,
        inline: Mapping[FunctionSpanKey, tuple[cs.NodeLabel, str]],
    ) -> tuple[cs.NodeLabel, str]:
        # Attribution ladder: the handler function (an inline one by where
        # it starts, a named one in the registering scope before the
        # module), else the registering call's enclosing function, else the
        # module, so the endpoint stays anchored and the trace lands at the
        # wiring site.
        if registration.handler_start is not None:
            row, col = registration.handler_start
            source = inline.get((module_qn, row + 1, col))
            if source is not None:
                return source
        registry = self._factory.definition_processor.function_registry
        scope_qn = (
            f"{module_qn}{cs.SEPARATOR_DOT}{registration.scope}"
            if registration.scope
            else None
        )
        candidates = []
        if registration.handler_name:
            if scope_qn is not None:
                candidates.append(
                    f"{scope_qn}{cs.SEPARATOR_DOT}{registration.handler_name}"
                )
            candidates.append(
                f"{module_qn}{cs.SEPARATOR_DOT}{registration.handler_name}"
            )
        if scope_qn is not None:
            candidates.append(scope_qn)
        for qn in candidates:
            node_type = registry.get(qn)
            if node_type is not None:
                return _route_label(node_type), qn
        return cs.NodeLabel.MODULE, module_qn

    def _route_module_asts(
        self, run: EndpointRun, only: set[str] | None = None
    ) -> dict[str, tuple[Node, cs.SupportedLanguage, Path]]:
        dp = self._factory.definition_processor
        files: dict[str, Path] = dict(dp.module_qn_to_file_path)
        if only is None:
            for qn, path in self._graph_route_module_paths(run):
                files.setdefault(qn, path)
        else:
            files = {qn: p for qn, p in files.items() if qn in only}
        out: dict[str, tuple[Node, cs.SupportedLanguage, Path]] = {}
        for qn, path in files.items():
            entry = self._ast_cache.load(path)
            if entry is not None and entry[1] in ROUTE_CALL_LANGUAGES:
                out[qn] = (entry[0], entry[1], path)
        return out

    def _is_test_module(self, path: Path | None) -> bool:
        # Judged on the repo-relative POSIX path; an unknown path (a
        # rehydrated legacy handler) stays permissive. The repo-relative cut
        # keeps a `/tmp/pytest-*/` parent from classifying the whole run.
        if path is None:
            return False
        try:
            relative = path.relative_to(self._repo_path)
        except ValueError:
            # Outside the repo means the repo-relative judgement is
            # impossible; stay permissive rather than matching absolute
            # segments like a `/tmp/tests/` parent.
            return False
        return matches_test_path(relative.as_posix())

    def _graph_route_module_paths(self, run: EndpointRun) -> list[tuple[str, Path]]:
        # Unchanged modules come back from the graph on an incremental run,
        # already narrowed to route-capable extensions at the query.
        graph = self._graph
        if graph is None:
            return []
        params = {
            cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT,
            "extensions": list(ROUTE_MODULE_EXTENSIONS),
        }
        try:
            rows = list(graph.fetch_all(CYPHER_PROJECT_MODULES, params))
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.debug(ls.GRAPH_READ_UNAVAILABLE, label="route modules", error=exc)
            return []
        out: list[tuple[str, Path]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qn, rel_path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
            # `svc.` also selects `svc.v2`'s modules (issues #2126, #1991).
            if isinstance(qn, str) and isinstance(rel_path, str) and self._owns(qn):
                out.append((qn, self._repo_path / rel_path))
        # These modules feed the EXPOSES cleanup, a DELETE. With the registry
        # unread, ownership degrades to the prefix rule and `svc.v2`'s
        # modules pass as `svc`'s, so read nothing, as a failed module read
        # does; the next healthy run cleans up (PR #2129).
        if run.registry_unread:
            return []
        return out

    def _rehydrated_endpoint_entries(
        self,
        entries: list[tuple[cs.NodeLabel, str, list[str], str | None]],
        only: set[str] | None,
        known_files: Mapping[str, Path],
        module_asts: Mapping[str, Node],
    ) -> list[tuple[cs.NodeLabel, str, list[str], str | None]]:
        pending_qns = {qn for _label, qn, _decorators, _module in entries}
        if only is None:
            return self._rehydrated_route_handlers(pending_qns, set(module_asts))
        # Scoped: only the handlers of the router modules pulled in above; a
        # module in `only` re-parsed and queued its own. Handlers are
        # attributed against every module the graph knows, never the partial
        # scoped set, or a package `__init__` pulled in by an import would
        # stand in for its unloaded children and claim unrelated routers'
        # handlers.
        return [
            entry
            for entry in self._rehydrated_route_handlers(pending_qns, set(known_files))
            if entry[3] in module_asts and entry[3] not in only
        ]

    def _rehydrated_route_handlers(
        self, already_pending: set[str], module_qns: set[str]
    ) -> list[tuple[cs.NodeLabel, str, list[str], str | None]]:
        graph = self._graph
        if graph is None:
            return []
        params = {cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT}
        try:
            rows = list(graph.fetch_all(CYPHER_PROJECT_ROUTE_HANDLERS, params))
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.debug(ls.GRAPH_READ_UNAVAILABLE, label="route handlers", error=exc)
            return []
        out: list[tuple[cs.NodeLabel, str, list[str], str | None]] = []
        # `svc.` also selects `svc.v2`'s handlers (issue #2126).
        owned = [
            row
            for row in rows
            if isinstance(qn := row.get(cs.KEY_QUALIFIED_NAME), str) and self._owns(qn)
        ]
        for row in owned:
            if not isinstance(row, dict):
                continue
            entry = _route_handler_entry(row, already_pending, module_qns)
            if entry is not None:
                out.append(entry)
        return out

    def _python_module_files(self) -> dict[str, Path]:
        # Re-parsed files first; on an incremental run the unchanged modules
        # come back from the graph's Module nodes.
        dp = self._factory.definition_processor
        files: dict[str, Path] = {
            qn: path
            for qn, path in dp.module_qn_to_file_path.items()
            if path.suffix == ".py"
        }
        for qn, path in self._graph_python_modules():
            files.setdefault(qn, path)
        return files

    def _python_module_asts(self, files: Mapping[str, Path]) -> dict[str, Node]:
        # Loaded through the disk-backed AST cache.
        asts: dict[str, Node] = {}
        for qn, path in files.items():
            entry = self._ast_cache.load(path)
            if entry is not None and entry[1] == cs.SupportedLanguage.PYTHON:
                asts[qn] = entry[0]
        return asts

    def _graph_python_modules(self) -> list[tuple[str, Path]]:
        graph = self._graph
        if graph is None:
            return []
        params = {cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT}
        try:
            rows = list(graph.fetch_all(CYPHER_PROJECT_PY_MODULES, params))
        except Exception as exc:  # noqa: BLE001 -- any engine's driver error; the read degrades
            logger.debug(ls.GRAPH_READ_UNAVAILABLE, label="Python modules", error=exc)
            return []
        out: list[tuple[str, Path]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            qn, rel_path = row.get(cs.KEY_QUALIFIED_NAME), row.get(cs.KEY_PATH)
            # `svc.` also selects `svc.v2`'s modules (issues #2126, #1991).
            if isinstance(qn, str) and isinstance(rel_path, str) and self._owns(qn):
                out.append((qn, self._repo_path / rel_path))
        return out

    def link_endpoint_resources(
        self,
        exclude_paths: frozenset[str] | None,
        unignore_paths: frozenset[str] | None,
    ) -> None:
        # After flush_all so this run's Resource nodes are queryable; NETWORK
        # resources of previously indexed projects join here too, which is
        # what makes client-URL-to-endpoint edges cross-project (issue #425).
        # The raw ingestor bypasses the capture filter, so gate explicitly.
        if not self._capture.rel_enabled(cs.RelationshipType.RESOLVES_TO):
            return
        if not isinstance(self._ingestor, QueryingIngestorProtocol):
            return
        created = link_endpoints(self._ingestor)
        if created:
            logger.info(ls.CLIENT_URLS_RESOLVED.format(count=created))
            self._ingestor.flush_all()
        # Contract files name the operations the generated artefacts on both
        # sides implement, so they anchor after the artefacts exist.
        anchored = link_contracts(
            self._ingestor,
            self._repo_path,
            self._project_name,
            exclude_paths,
            unignore_paths,
        )
        if anchored:
            logger.info(ls.CONTRACT_ARTEFACTS_ANCHORED.format(count=anchored))
            self._ingestor.flush_all()
