"""Read back from the graph what an incremental sync did not re-parse: the
definitions, module qns, inheritance and implementer pairs, the type facts
waiting on them, and the location indexes the semantic joins resolve against."""

import os
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Protocol

from loguru import logger
from tree_sitter import Parser

from .. import constants as cs
from .. import logs as ls
from ..capture import CaptureSelection
from ..function_registry import FunctionRegistryTrie
from ..language_spec import (
    csharp_namespaced_from_graph,
    csharp_partial_key_from_graph,
    get_language_for_extension,
)
from ..parsers import utils as parser_utils
from ..parsers.csharp import utils as csharp_utils
from ..parsers.factory import ProcessorFactory
from ..parsers.field_nodes import PendingFieldType
from ..parsers.go import interfaces as go_ifaces
from ..parsers.go import read_package_clause
from ..parsers.go import utils as go_utils
from ..parsers.go.interfaces import GoInterfaceDecl, GoMethodSpec, GoSignature
from ..parsers.js_ts.utils import mark_js_ts_module_private
from ..parsers.parameter_nodes import PendingParameterType
from ..parsers.rust import rehydration as rs_rehydration
from ..services import QueryProtocol
from ..types_defs import (
    CppDefinitionSpan,
    FunctionLocation,
    FunctionLocations,
    PendingTypeFact,
    PropertyParams,
    ResultRow,
)
from ..utils.path_utils import base_module_qn, is_inline_module_path

_CPP_SPAN_FILE_EXTENSIONS = frozenset(cs.CPP_EXTENSIONS) | frozenset(cs.C_EXTENSIONS)


class RehydrationRun(Protocol):
    """What the read-back takes from the sync run it is part of: whether the
    run re-parses every file, which lets a failed read degrade where an
    incremental run must abort, and the inbound edges `run` holds for its
    restore, which count as read when the implementer pairs are rebuilt."""

    is_full_build: bool
    pending_inbound_edges: list[ResultRow]


def _persisted_int(value: object) -> int | None:
    # bool is an int subclass; a stray True would key as 1 and shadow a real
    # entry, so it is rejected explicitly (same discipline as the C# path).
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _stored_strings(value: object) -> list[str]:
    # A stored list property; absent, or of another type, reads as empty.
    return (
        [item for item in value if isinstance(item, str)]
        if isinstance(value, list)
        else []
    )


def _stored_language(path: object) -> cs.SupportedLanguage | None:
    # The language of a stored definition, from its file's extension.
    return (
        get_language_for_extension(PurePosixPath(path).suffix)
        if isinstance(path, str)
        else None
    )


def _is_rust_method_edge(row: ResultRow) -> bool:
    """A captured edge between two Methods out of a Rust file."""
    method = cs.NodeLabel.METHOD.value
    path = row.get(cs.KEY_CALLER_PATH)
    return (
        row.get(cs.KEY_CALLER_LABEL) == method
        and row.get(cs.KEY_TARGET_LABEL) == method
        and isinstance(path, str)
        and path.endswith(cs.EXT_RS)
    )


def _carried_pair_rows(
    captured: Iterable[ResultRow],
) -> tuple[list[ResultRow], list[ResultRow]]:
    """Captured inbound IMPLEMENTS and Rust OVERRIDES edges, as read-back rows."""
    # Spelled as CYPHER_ALL_IMPLEMENTS and CYPHER_ALL_RUST_OVERRIDES return
    # them, so the read-back treats an edge it will restore like one it read.
    implements: list[ResultRow] = []
    overrides: list[ResultRow] = []
    for row in captured:
        rel = row.get(cs.KEY_REL)
        source, target = row.get(cs.KEY_CALLER_QN), row.get(cs.KEY_TARGET_QN)
        if rel == cs.RelationshipType.IMPLEMENTS.value:
            implements.append({cs.KEY_CHILD_QN: source, cs.KEY_BASE_QN: target})
        elif rel == cs.RelationshipType.OVERRIDES.value and _is_rust_method_edge(row):
            overrides.append({cs.KEY_FROM_QN: source, cs.KEY_TO_QN: target})
    return implements, overrides


def _is_reread_cpp_definition(row: ResultRow, reparsed_keys: Collection[str]) -> bool:
    """A C++ member known only from a definition this run re-parses.

    Its node hangs off a class in an unchanged header, so the graph still
    holds it, but its definition is being read again and will register
    whatever it is now. Read back as well, it would be the name a changed
    or newly added overload is measured against, and a clean index has
    no such stale entry (issue #2455). A member its class body declared
    stays: that declaration is not re-read.
    """
    return (
        isinstance(row.get(cs.KEY_SIGNATURE), str)
        and not row.get(cs.KEY_DECLARED_IN_CLASS)
        and row.get(cs.KEY_PATH) in reparsed_keys
    )


def rehydrated_bases_by_child(
    rows: list[ResultRow], existing: dict[str, list[str]]
) -> dict[str, list[str]]:
    # Group persisted INHERITS rows into child -> ordered bases, restoring
    # source order from base_index. Skip children already present locally
    # (freshly re-parsed). A class with more than one base needs a reliable
    # order (method resolution / override attribution are first-match-wins
    # over the base list); if any of its edges lacks a base_index (e.g. an
    # INHERITS written by an older index before base_index existed) the
    # order cannot be trusted, so that class is NOT rehydrated and falls
    # back to name-based resolution rather than risk the wrong base.
    # Single-base classes are order-independent and always safe.
    collected: dict[str, list[tuple[int | None, str]]] = {}
    for row in rows:
        child = row.get(cs.KEY_CHILD_QN)
        base = row.get(cs.KEY_BASE_QN)
        if not isinstance(child, str) or not isinstance(base, str):
            continue
        if child in existing:
            continue
        raw_index = row.get(cs.KEY_BASE_INDEX)
        index = raw_index if isinstance(raw_index, int) else None
        collected.setdefault(child, []).append((index, base))
    result: dict[str, list[str]] = {}
    for child, pairs in collected.items():
        if len(pairs) > 1 and any(index is None for index, _ in pairs):
            continue
        pairs.sort(key=lambda pair: (pair[0] is None, pair[0] or 0))
        result[child] = [base for _index, base in pairs]
    return result


def _go_type_location(
    row: ResultRow,
) -> tuple[tuple[str, int, int], tuple[str, str]] | None:
    """The (path, line, col) -> (qn, label) entry a Go type row restores."""
    path = row.get(cs.KEY_PATH)
    qn = row.get(cs.KEY_QUALIFIED_NAME)
    label = row.get(cs.KEY_LABEL)
    start_line = _persisted_int(row.get(cs.KEY_START_LINE))
    start_col = _persisted_int(row.get(cs.KEY_START_COL))
    if not (
        isinstance(path, str)
        and path.endswith(cs.EXT_GO)
        and isinstance(qn, str)
        and isinstance(label, str)
        and start_line is not None
        and start_col is not None
    ):
        return None
    return (path, start_line, start_col), (qn, label)


class GraphRehydrator:
    """The read-back that completes an incremental sync's view of the files
    it did not re-parse.

    Each read is scoped to this project; `owned_rows` keeps the rows whose
    qualified name this project, and not one nested under it, owns. A failed
    read degrades on a full build, which parsed every file anyway, and aborts
    an incremental run, whose cross-file edges depend on it. `module_qns`
    holds the module qns read back for files the run did not parse, and
    `cpp_spans` the C/C++ definition spans the hybrid expansion-call join
    reads for them.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        function_registry: FunctionRegistryTrie,
        parsers: Mapping[cs.SupportedLanguage, Parser],
        repo_path: Path,
        project_name: str,
        capture: CaptureSelection,
        owns: Callable[[str], bool],
        owned_rows: Callable[[list[ResultRow], str], list[ResultRow]],
    ) -> None:
        self._graph = graph
        self._factory = factory
        self._function_registry = function_registry
        self._parsers = parsers
        self._repo_path = repo_path
        self._project_name = project_name
        self._capture = capture
        self._owns = owns
        self._owned_rows = owned_rows
        # Module qns read back from the graph on incremental runs; deferred
        # import verification counts them as real internal targets.
        self.module_qns: set[str] = set()
        # Definition spans read back from the graph on incremental runs:
        # an expansion call's CALLEE may live in an UNCHANGED file whose
        # spans Pass 2 never recorded this run.
        self.cpp_spans: dict[str, list[CppDefinitionSpan]] = {}
        # path -> module qualified name as the graph records it, read once
        # per rehydration for the requeues (issue #1935).
        self._module_qns_by_path: dict[str, str] | None = None
        # Go files whose `package` clause a rehydration already read back.
        self._go_clauses_read: set[str] = set()

    def recorded_module_qn(self, path: str, run: RehydrationRun) -> str:
        """The module qualified name the graph records for `path`.

        A requeued type fact must carry the owner the clean pass gave it.
        `base_module_qn` runs BEFORE disambiguation, so it cannot reproduce
        a suffixed name (`proj.lib.d.ts` beside `proj.lib`, `proj.settings.py`
        beside `proj.settings`): the fact would then resolve its annotation
        through the BARE module's scope and gain an edge to the other file's
        type, an edge a clean index never has (issue #1935). The graph knows
        the answer; it is read once per rehydration, and the derivation is
        only the fallback for a path the graph does not hold.
        """
        if self._module_qns_by_path is None:
            self._module_qns_by_path = self._read_module_qns_by_path(run)
        recorded = self._module_qns_by_path.get(path)
        if recorded is not None:
            return recorded
        return base_module_qn(Path(path), self._project_name)

    def _read_module_qns_by_path(self, run: RehydrationRun) -> dict[str, str]:
        # Scoped in the query: the shared graph holds every project, and a
        # module of another project may record the same relative path.
        # A bodied Rust inline module (`mod alpha { .. }`) records its
        # enclosing FILE's path too, so several modules can share one path;
        # the clean pass owns a definition by the file module, which is the
        # shortest of those names (the inline ones nest under it), so the
        # shortest wins here and the requeue agrees with a clean index
        # (bot review on PR #1967: keyed on the last row returned, an
        # unrelated edit rehydrated `alpha.make` under `beta`).
        graph = self._graph
        if graph is None:
            return {}
        # The same posture as every other read in `rehydrate_registry_from_graph`
        #: a full build parsed every file and degrades to the
        # bare derivation with a warning; an incremental run would requeue
        # under the wrong owner, so the outage aborts it.
        try:
            rows = graph.fetch_all(
                cs.CYPHER_PROJECT_MODULE_PATHS,
                {
                    cs.KEY_PROJECT_NAME: self._project_name,
                    cs.KEY_PROJECT_PREFIX: f"{self._project_name}{cs.SEPARATOR_DOT}",
                },
            )
        except Exception as exc:
            if not run.is_full_build:
                # The module paths are unknown, the same outage the reingest
                # guard reports, so it says so rather than leaking the raw error.
                raise RuntimeError(ls.REINGEST_MODULE_PATHS_UNKNOWN) from exc
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return {}
        found: dict[str, str] = {}
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            path = row.get(cs.KEY_PATH)
            if not isinstance(qn, str) or not isinstance(path, str) or not path:
                continue
            if is_inline_module_path(path):
                continue
            # The read is prefix-scoped, and `svc.` selects `svc.v2`'s
            # modules too; a nested project's module must not become this
            # file's recorded owner (#1970).
            if not self._owns(qn):
                continue
            if path not in found or (len(qn), qn) < (len(found[path]), found[path]):
                found[path] = qn
        return found

    def _requeue_type_facts(
        self,
        run: RehydrationRun,
        node_type: cs.NodeLabel,
        qn: str,
        path: str,
        row: ResultRow,
    ) -> None:
        if node_type not in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD):
            return
        return_type = row.get(cs.KEY_RETURN_TYPE)
        param_types = row.get(cs.KEY_PARAM_TYPES)
        if not return_type and not param_types:
            return
        self._factory.definition_processor.pending_type_facts.append(
            PendingTypeFact(
                node_type.value,
                qn,
                self.recorded_module_qn(path, run),
                return_type if isinstance(return_type, str) else None,
                [str(p) for p in param_types]
                if isinstance(param_types, list)
                else None,
                path,
            )
        )

    def _requeue_member_types[P: (PendingParameterType, PendingFieldType)](
        self,
        run: RehydrationRun,
        reparsed_keys: Collection[str],
        query: str,
        project_params: PropertyParams,
        pending: list[P],
        make: Callable[[str, str, str, str], P],
    ) -> None:
        # OF_TYPE for the Parameter and Field nodes of files this run does not
        # re-parse: their owners never re-emit them, so the pending list is
        # rebuilt from the graph exactly as _requeue_type_facts rebuilds
        # RETURNS/ACCEPTS (issue #1527). Without this, re-parsing only the
        # TYPE's file detaches every OF_TYPE into it and nothing puts them
        # back. The key is the FILE: a member whose file is being re-parsed
        # this run is re-emitted by ingest and queues its own (a requeue here
        # would carry the OLD annotation); one whose file is not needs
        # rebuilding. Neither registry membership nor "what the rehydration
        # loop restored" can tell the two apart -- a reused updater already
        # holds every unchanged definition, a fresh one holds none -- and both
        # were tried first.
        graph = self._graph
        if (
            run.is_full_build
            or not self._capture.rel_enabled(cs.RelationshipType.OF_TYPE)
            or graph is None
        ):
            # A full build re-parses every file: ingest queues everything.
            return
        rows = self._owned_rows(
            graph.fetch_all(query, project_params),
            cs.KEY_QUALIFIED_NAME,
        )
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            type_name = row.get(cs.KEY_TYPE_NAME)
            path = row.get(cs.KEY_PATH)
            if not (
                isinstance(qn, str)
                and isinstance(type_name, str)
                and isinstance(path, str)
            ):
                continue
            if path in reparsed_keys:
                continue
            pending.append(
                make(qn, self.recorded_module_qn(path, run), type_name, path)
            )

    def rehydrate_registry_from_graph(
        self, run: RehydrationRun, reparsed_keys: Collection[str]
    ) -> None:
        # Incremental runs populate the function registry only from re-parsed
        # files. Read every definition's qualified name back from the graph and
        # re-register the ones missing locally, so calls and instantiations
        # into files that were not re-parsed still resolve and their edges are
        # re-emitted. Without this, editing one file drops cross-file CALLS /
        # INSTANTIATES into any unchanged file (issue #532, outbound half).
        graph = self._graph
        if graph is None:
            return
        self._go_clauses_read.clear()
        # Read afresh each run: a module may have gained or lost its suffix
        # since the last one, and the requeues below all consult this.
        self._module_qns_by_path = None
        project_params = {cs.KEY_PROJECT_PREFIX: self._project_name + "."}
        try:
            rows = self._owned_rows(
                graph.fetch_all(cs.CYPHER_ALL_DEFINITION_QNS, project_params),
                cs.KEY_QUALIFIED_NAME,
            )
        except Exception:
            # Rehydration completes cross-file resolution for files this run
            # did not re-parse: a FULL build parsed them all, so it degrades
            # to the freshly parsed registry; an incremental run would drop
            # cross-file edges, so the outage aborts it.
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        restored: list[ResultRow] = []
        for row in rows:
            if self._rehydrate_definition_row(run, reparsed_keys, row):
                restored.append(row)
        if restored:
            logger.info(ls.REGISTRY_REHYDRATED, count=len(restored))
        # After the loop: whether a method's owner is a trait needs the whole
        # registry, and its rows arrive in no particular order.
        rs_rehydration.rehydrate_return_types(
            restored,
            self._parsers,
            self._function_registry,
            self._factory.facts.method_return_types,
        )
        self._rehydrate_go_method_sets(run, restored)
        # Module qns from unchanged files: deferred import verification and
        # C++20 module-impl resolution must count them as real targets, or
        # an incremental run would drop edges a clean index emits.
        try:
            module_rows = self._owned_rows(
                graph.fetch_all(cs.CYPHER_ALL_MODULE_QNS, project_params),
                cs.KEY_QUALIFIED_NAME,
            )
        except Exception:
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        self._rehydrate_module_qns(module_rows)
        self.rehydrate_class_inheritance_from_graph(run)
        # run() holds the inbound edges for the restore after the deferred
        # stages (#3270), so an unchanged impl of a re-parsed trait that only
        # its OVERRIDES edge records (`impl crate::Trait for u64`) is not in
        # the graph yet: the held rows count as read, as reingest's captured
        # ones do.
        self.rehydrate_interface_implementers(
            run, project_params, carried=run.pending_inbound_edges
        )
        dp = self._factory.definition_processor
        self._requeue_member_types(
            run,
            reparsed_keys,
            cs.CYPHER_PROJECT_PARAMETER_TYPES,
            project_params,
            dp.pending_parameter_types,
            PendingParameterType,
        )
        self._requeue_member_types(
            run,
            reparsed_keys,
            cs.CYPHER_PROJECT_FIELD_TYPES,
            project_params,
            dp.pending_field_types,
            PendingFieldType,
        )

    def rehydrate_interface_implementers(
        self,
        run: RehydrationRun,
        project_params: PropertyParams,
        carried: Iterable[ResultRow] = (),
    ) -> None:
        # Parsing records implementer -> interface pairs only for the files it
        # reads. A call typed to an interface with ONE implementer also reaches
        # that implementer's method, so a run that re-parsed only the caller
        # saw no implementer and dropped the edge a clean index emits: gson's
        # getFieldNames into FieldNamingPolicy, serde_json's
        # u64::error_halfscale() (issue #2403).
        graph = self._graph
        if graph is None:
            return
        try:
            implements = self._owned_rows(
                graph.fetch_all(cs.CYPHER_ALL_IMPLEMENTS, project_params),
                cs.KEY_CHILD_QN,
            )
            rust_overrides = self._owned_rows(
                graph.fetch_all(cs.CYPHER_ALL_RUST_OVERRIDES, project_params),
                cs.KEY_FROM_QN,
            )
            required = self._owned_rows(
                graph.fetch_all(
                    cs.CYPHER_RUST_TRAITS_WITH_REQUIRED_METHODS, project_params
                ),
                cs.KEY_QUALIFIED_NAME,
            )
        except Exception:
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        carried_implements, carried_overrides = _carried_pair_rows(carried)
        implements += self._owned_rows(carried_implements, cs.KEY_CHILD_QN)
        rust_overrides += self._owned_rows(carried_overrides, cs.KEY_FROM_QN)
        pairs = [
            (child, base)
            for row in implements
            if isinstance(child := row.get(cs.KEY_CHILD_QN), str)
            and isinstance(base := row.get(cs.KEY_BASE_QN), str)
        ]
        pairs.extend(
            rs_rehydration.nodeless_impls(rust_overrides, self._function_registry)
        )
        closed = {
            qn
            for row in required
            if isinstance(qn := row.get(cs.KEY_QUALIFIED_NAME), str)
        }
        # Rebuilt, not accumulated, and cleared only once the rows are in
        # hand: the _rehydrate_module_qns posture.
        rehydrated = self._factory.facts.rehydrated_interface_implementers
        rehydrated.clear()
        definition_paths = (
            self._factory.definition_processor.facts.rehydrated_definition_paths
        )
        for implementer, interface in pairs:
            if not rs_rehydration.may_hide_impls(interface, closed, definition_paths):
                rehydrated.setdefault(interface, set()).add(implementer)

    def _rehydrate_module_qns(self, module_rows: list[ResultRow]) -> None:
        """Rebuild the module and interface qn sets from the graph's rows."""
        # Rebuild rather than accumulate. A reused updater runs this on every
        # incremental pass, and remove_file_from_state only discards the qns
        # this updater removed itself; a Module deleted from the graph by
        # anything else (a second updater, delete_project then a re-index)
        # would linger here and keep being offered to deferred import
        # verification as a live target. Cleared only now that the rows are
        # in hand, so a failed query above leaves the previous set intact.
        self.module_qns.clear()
        # Same rebuild for the interface set the loop below fills from these
        # same rows. A stale entry defeats the membership check in
        # resolve_deferred_cpp_module_impls, which is what stops an IMPLEMENTS
        # edge being minted to a ModuleInterface the graph no longer holds.
        #
        # Interfaces THIS run parsed are put back: their node writes may still
        # be in the ingestor's buffer and so absent from the rows just
        # fetched, and dropping one loses the IMPLEMENTS edge for an interface
        # and implementation added together. That is not hypothetical -- an
        # earlier attempt at this cleared the set outright and did exactly
        # that.
        dp = self._factory.definition_processor
        dp.cpp_module_interfaces.clear()
        dp.cpp_module_interfaces |= dp.cpp_interfaces_parsed_this_run
        for row in module_rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            if not isinstance(qn, str) or not isinstance(label, str):
                continue
            if label == cs.NodeLabel.MODULE_INTERFACE.value:
                self._factory.definition_processor.cpp_module_interfaces.add(qn)
            else:
                self.module_qns.add(qn)
            # An unchanged PHP file's declared namespace, which a
            # `use function App\\Util\\slug` is matched against (issue #3277).
            if (
                isinstance(namespace := row.get(cs.KEY_PHP_NAMESPACE), str)
                and namespace
            ):
                self._factory.import_processor.php.php_module_namespaces[qn] = namespace

    def _rehydrate_definition_row(
        self, run: RehydrationRun, reparsed_keys: Collection[str], row: ResultRow
    ) -> bool:
        """Re-register one definition row missing locally; True when added."""
        qn = row.get(cs.KEY_QUALIFIED_NAME)
        label = row.get(cs.KEY_LABEL)
        if not isinstance(qn, str) or not isinstance(label, str):
            return False
        if _is_reread_cpp_definition(row, reparsed_keys):
            return False
        try:
            node_type = cs.NodeLabel(label)
        except ValueError:
            return False
        # A TS type and a value hold one qualified name under two labels
        # (issue #2520). Skipping the second row left whichever the store
        # returned first as the name's only kind, so an `interface Box` read
        # back before `class Box` made every `new Box()` bind the interface.
        if qn in self._function_registry and not (
            self._function_registry.claim_other_space(qn, node_type)
        ):
            return False
        self._function_registry[qn] = node_type
        # A `@line` variant rejoins its natural qn's duplicate list: a call
        # into the name fans out to every same-named definition, and one read
        # back as a plain entry left the incremental graph with a single edge
        # where a clean index has one per definition (issue #2403).
        self._function_registry.restore_variant(qn)
        self._restore_registry_marks(qn, row)
        self._restore_resolution_indexes(qn, row)
        # Record the defining file so _is_cpp_defined can language-check
        # rehydrated candidates (deferred C++ INHERITS resolution runs
        # after this and must reach bases in UNCHANGED headers).
        if isinstance(path := row.get(cs.KEY_PATH), str):
            self._rehydrate_definition_path(run, node_type, qn, path, row)
        return True

    def _restore_registry_marks(self, qn: str, row: ResultRow) -> None:
        """Restore what the registry records of a definition beyond its kind:
        the names it is indexed by and the marks resolution consults."""
        # Parsing indexes a definition under its name as well as its qn's
        # last segment, which for `ILog.Info(string)` is not `Info` and for
        # the variant `Limb@6` is not `Limb`; without it a re-parsed caller's
        # name lookup misses the definition (#3261, #2403).
        if isinstance(name := row.get(cs.KEY_NAME), str) and name:
            self._function_registry.index_name(qn, name)
        # Restore the property-name set for unchanged files: property-dispatch
        # resolution (`obj.prop`) consults it, so a re-parsed file's call to a
        # @property defined elsewhere would otherwise drop.
        if row.get(cs.KEY_IS_PROPERTY):
            self._function_registry.mark_property(qn)
        # Restore the abstract mark of a member that never runs (a TS
        # `abstract` member, an @abstractmethod stub): without it, an untyped
        # call that sees it beside its one override reads as ambiguous after a
        # caller-only sync and loses the edge a fresh index gives it (#2524).
        if parser_utils.is_stored_abstract_member(
            _stored_strings(row.get(cs.KEY_DECORATORS)),
            _stored_strings(row.get(cs.KEY_MODIFIERS)),
            _stored_language(row.get(cs.KEY_PATH)),
        ):
            self._function_registry.mark_abstract(qn)
        # Restore the object-member set for unchanged files, or a re-parsed
        # file's bare call binds by name to a JS/TS object literal's function
        # value that only its object reaches (issue #2435).
        if row.get(cs.KEY_IS_OBJECT_MEMBER):
            self._function_registry.mark_object_member(qn)
        # Restore a C++ member's overload signature for unchanged headers, or
        # a re-parsed out-of-class definition cannot tell which overload it
        # defines and lands every one on the plain-named node (issue #2455).
        if isinstance(signature := row.get(cs.KEY_SIGNATURE), str):
            self._function_registry.restore_overload(
                qn, signature, bool(row.get(cs.KEY_DECLARED_IN_CLASS))
            )
        # Restore the class-body aliases of an unchanged method (`run =
        # _plain`), or a re-parsed file's `obj.run()` binds to nothing
        # (issue #2620).
        if isinstance(aliases := row.get(cs.KEY_MEMBER_ALIASES), list):
            owner_qn = qn.rsplit(cs.SEPARATOR_DOT, 1)[0]
            for alias in aliases:
                self._function_registry.add_member_alias(
                    f"{owner_qn}{cs.SEPARATOR_DOT}{alias}", qn
                )
        # And the values an unchanged class body binds (`run = None`), or a
        # re-parsed file's `Sub().run()` reaches the base's `run` they hide.
        if isinstance(names := row.get(cs.KEY_NON_METHOD_MEMBERS), list):
            self._function_registry.set_non_method_members(
                qn, [name for name in names if isinstance(name, str)]
            )

    def _restore_resolution_indexes(self, qn: str, row: ResultRow) -> None:
        """Rejoin a definition to the name sets a language's calls resolve
        through: macros, body-scoped names and C# extension methods."""
        # Restore the macro-namespace set for unchanged files: the Rust
        # macro/fn gate consults it, so a re-parsed file's invocation of a
        # macro defined elsewhere would otherwise drop.
        if row.get(cs.KEY_IS_MACRO):
            self._factory.facts.macro_qns.add(qn)
        # Restore the body-scoped-name set for unchanged files, or a
        # re-parsed file's bare call binds by name to a function expression
        # only its own body can call by that name (issue #2402).
        if row.get(cs.KEY_IS_BODY_SCOPED_NAME):
            self._function_registry.mark_body_scoped_name(qn)
        # Restore a C# extension method's index entry for unchanged files, or
        # a re-parsed `b.Describe()` finds no extension and gets no edge
        # (issue #3275).
        if isinstance(receiver := row.get(cs.KEY_EXTENSION_RECEIVER), str) and receiver:
            namespace = row.get(cs.KEY_EXTENSION_NAMESPACE)
            arity = row.get(cs.KEY_EXTENSION_RECEIVER_ARITY)
            csharp_utils.add_extension_method(
                self._factory.facts.csharp_extension_methods,
                qn,
                receiver,
                namespace if isinstance(namespace, str) else "",
                arity if isinstance(arity, int) else 0,
            )

    def _rehydrate_go_method_sets(
        self, run: RehydrationRun, rows: list[ResultRow]
    ) -> None:
        # An unchanged Go file's methods and interfaces still count toward
        # implicit satisfaction (issue #2483): a re-parsed type implements an
        # interface it does not touch. The signature is what the parse
        # records, `param_types` and `return_type` (its result list parsed
        # again, as the file's parse read it), and the embedded types are
        # the interface's own `embeds`. An interface stored without that list
        # (an older or imported graph) has a method set nobody recorded, so it
        # is left out rather than read as embedding nothing.
        dp = self._factory.definition_processor
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            if not isinstance(qn, str) or (
                _stored_language(row.get(cs.KEY_PATH)) != cs.SupportedLanguage.GO
            ):
                continue
            path = str(row.get(cs.KEY_PATH))
            package = go_utils.package_key(
                path,
                dp.facts.go_package_names.get(self.recorded_module_qn(path, run)),
            )
            label = row.get(cs.KEY_LABEL)
            if label == cs.NodeLabel.METHOD and isinstance(
                param_types := row.get(cs.KEY_PARAM_TYPES), list
            ):
                result = row.get(cs.KEY_RETURN_TYPE)
                dp.go_methods[qn] = GoMethodSpec(
                    GoSignature(
                        tuple(_stored_strings(param_types)),
                        go_ifaces.stored_result_types(
                            result if isinstance(result, str) else "",
                            self._parsers.get(cs.SupportedLanguage.GO),
                        ),
                    ),
                    package,
                    path,
                )
            elif label == cs.NodeLabel.INTERFACE and isinstance(
                embeds := row.get(cs.KEY_EMBEDS), list
            ):
                dp.go_interfaces[qn] = GoInterfaceDecl(
                    tuple(_stored_strings(embeds)), package, path
                )

    def _rehydrate_definition_path(
        self,
        run: RehydrationRun,
        node_type: cs.NodeLabel,
        qn: str,
        path: str,
        row: ResultRow,
    ) -> None:
        """Restore the path-keyed state of a rehydrated definition."""
        self._factory.facts.rehydrated_definition_paths[qn] = path
        # A private declaration in an unchanged star source must stay private,
        # or it counts as re-exported and makes the exported one a barrel
        # names ambiguous.
        mark_js_ts_module_private(
            self._function_registry,
            qn,
            # A row written before the flag existed reads as exported: the
            # star lookup then counts it, as it did before.
            row.get(cs.KEY_IS_EXPORTED) is not False,
            get_language_for_extension(Path(path).suffix),
        )
        # The C# declared-form index (issue #1629) is filled only by
        # parsing; an unchanged type must stay reachable by `N.Widget`
        # from a re-parsed base list or receiver.
        if node_type not in (
            cs.NodeLabel.FUNCTION,
            cs.NodeLabel.METHOD,
        ) and path.endswith(cs.EXT_CS):
            self._rehydrate_csharp_declared_form(run, qn, path, row)
        # Persisted annotations of UNCHANGED definitions rejoin the
        # type-edge queue (issue #1527): a changed file can add the
        # first resolvable type an old annotation names, and MERGE
        # makes re-emitting the already-known edges harmless.
        self._requeue_type_facts(run, node_type, qn, path, row)
        # Spans for hybrid expansion-call callee joins: only C/C++
        # Function/Method rows carry a usable span.
        start = row.get(cs.KEY_START_LINE)
        end = row.get(cs.KEY_END_LINE)
        if (
            node_type in (cs.NodeLabel.FUNCTION, cs.NodeLabel.METHOD)
            and isinstance(start, int)
            and isinstance(end, int)
            and os.path.splitext(path)[1].lower() in _CPP_SPAN_FILE_EXTENSIONS
        ):
            self.cpp_spans.setdefault(path, []).append(
                CppDefinitionSpan(start, end, node_type.value, qn)
            )
        if path.endswith(cs.EXT_GO):
            self._rehydrate_go_package_clause(run, path)

    def _rehydrate_go_package_clause(self, run: RehydrationRun, path: str) -> None:
        # Only parsing records a Go file's `package` clause, and Go package
        # scope is (directory, clause): without it an unchanged
        # `//go:build ignore` generator's `package main` beside `package p`
        # competed, on an incremental run, with the function a re-parsed
        # caller's bare call names (#2616). The file is unchanged
        # since the graph recorded it, so its clause is read back from disk.
        if path in self._go_clauses_read:
            return
        self._go_clauses_read.add(path)
        clauses = self._factory.facts.go_package_names
        module_qn = self.recorded_module_qn(path, run)
        if module_qn not in clauses and (
            clause := read_package_clause(self._repo_path / path)
        ):
            clauses[module_qn] = clause

    def _rehydrate_csharp_declared_form(
        self, run: RehydrationRun, qn: str, path: str, row: ResultRow
    ) -> None:
        """Rejoin an unchanged C# type to its declared-form indexes."""
        namespace = row.get(cs.KEY_NAMESPACE)
        namespaced = csharp_namespaced_from_graph(
            qn,
            path,
            self._project_name,
            namespace if isinstance(namespace, str) else None,
        )
        processor = self._factory.definition_processor
        # The arity a parse records, so `new PB<int>()` in a re-parsed file
        # finds the generic twin in an unchanged one (issue #2579).
        arity = row.get(cs.KEY_GENERIC_ARITY)
        arity = arity if isinstance(arity, int) else 0
        if arity:
            processor.facts.csharp_class_generic_arity[qn] = arity
            processor.facts.csharp_class_owner_module[qn] = self.recorded_module_qn(
                path, run
            )
        # Rejoin the partial group parsing would have given it,
        # under the same key, so a declared name spanning
        # unchanged parts stays one type; a lone type's group of
        # one reads exactly as no group.
        if key := csharp_partial_key_from_graph(qn, path, self._project_name, arity):
            group = processor._csharp_partial_index.setdefault(key, [])
            group.append(qn)
            processor.facts.csharp_partial_groups[qn] = group
        if namespaced:
            processor.facts.csharp_class_namespaced[qn] = namespaced
            processor.facts.csharp_namespaced_qns.setdefault(namespaced, set()).add(qn)

    def rehydrate_class_inheritance_from_graph(self, run: RehydrationRun) -> None:
        # Incremental runs rebuild class_inheritance only from re-parsed files.
        # Restore the child->bases map for classes defined in files that were
        # not re-parsed, so protocol dispatch and inherited-method resolution
        # work in Pass 3 (issue #532 residual). Only fill entries missing
        # locally: a re-parsed class already has its fresh, correctly ordered
        # bases, so we must not overwrite or duplicate them. CYPHER_ALL_INHERITS
        # is ordered by base_index, so a rehydrated class's bases keep their
        # original source order (multiple inheritance resolves the same base a
        # clean index would).
        graph = self._graph
        if graph is None:
            return
        class_inheritance = self._factory.facts.class_inheritance
        try:
            rows = self._owned_rows(
                graph.fetch_all(
                    cs.CYPHER_ALL_INHERITS,
                    {cs.KEY_PROJECT_PREFIX: self._project_name + "."},
                ),
                cs.KEY_CHILD_QN,
            )
        except Exception:
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        for child, bases in rehydrated_bases_by_child(rows, class_inheritance).items():
            class_inheritance[child] = bases

    def rehydrate_csharp_type_locations(self, run: RehydrationRun) -> None:
        # Incremental runs fill csharp_type_locations only from re-parsed files,
        # but csharp_joins.join_partials resolves every partial-declaration
        # location against it. Restore the (path, start_line) -> qn entries for
        # types in UNCHANGED .cs files from the persisted graph so a partial
        # part in an unchanged file still joins its group (issue #1229). A
        # re-parsed file's fresh Pass-2 entry is kept (not overwritten); a stale
        # rehydrated position for a type that moved is simply never queried by
        # the join.
        graph = self._graph
        if graph is None:
            return
        locations = self._factory.definition_processor.csharp_type_locations
        try:
            rows = self._owned_rows(
                graph.fetch_all(
                    cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS,
                    {cs.KEY_PROJECT_PREFIX: self._project_name + "."},
                ),
                cs.KEY_QUALIFIED_NAME,
            )
        except Exception:
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        restored = 0
        for row in rows:
            path = row.get(cs.KEY_PATH)
            start_line = row.get(cs.KEY_START_LINE)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            if not (
                isinstance(path, str)
                and isinstance(start_line, int)
                and not isinstance(start_line, bool)
                and isinstance(qn, str)
            ):
                # bool is an int subclass; a stray True would key as line 1 and
                # shadow a real line-1 type, so reject it explicitly.
                continue
            key = (path, start_line)
            if key not in locations:
                locations[key] = qn
                restored += 1
        if restored:
            logger.info(ls.CSHARP_TYPE_LOCATIONS_REHYDRATED.format(count=restored))

    def ungroup_rehydrated_non_partials(self) -> None:
        # The registry rehydration joins every unchanged C# type to the partial
        # group of its directory and declared name, as its rows do not say
        # whether the type was declared `partial`; parsing groups only the
        # types that are. `class Box` and `class Box<T>` side by side are two
        # types, and grouped they read as one candidate for a type reference:
        # an edge an incremental sync drew and a clean index does not (#2469).
        # Only rehydrated types are judged by the graph's rows: a re-parsed
        # one's row may still describe the source before the edit.
        dp = self._factory.definition_processor
        rehydrated = dp.facts.rehydrated_definition_paths
        graph = self._graph
        if graph is None or not any(
            len(group) > 1 and qn in rehydrated
            for qn, group in dp.facts.csharp_partial_groups.items()
        ):
            return
        rows = self._owned_rows(
            graph.fetch_all(
                cs.CYPHER_ALL_CSHARP_TYPE_LOCATIONS,
                {cs.KEY_PROJECT_PREFIX: self._project_name + cs.SEPARATOR_DOT},
            ),
            cs.KEY_QUALIFIED_NAME,
        )
        for row in rows:
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            modifiers = row.get(cs.KEY_MODIFIERS)
            if (
                isinstance(qn, str)
                and qn in rehydrated
                and isinstance(modifiers, list)
                and cs.TS_CSHARP_MODIFIER_PARTIAL not in modifiers
                and (group := dp.facts.csharp_partial_groups.pop(qn, None)) is not None
            ):
                # In place: the list is the one `_csharp_partial_index` holds.
                group[:] = [part for part in group if part != qn]

    def rehydrate_go_type_locations(self, run: RehydrationRun) -> None:
        # Incremental runs fill go_type_locations only from re-parsed files,
        # but go_joins.join_implements resolves BOTH ends of each pair against
        # it. Restore (path, line, col) -> (qn, label) for types in unchanged
        # .go files from the persisted graph (issue #1240). A pre-#1240 graph
        # has no start_col: those rows are skipped and behavior degrades to
        # today's until a re-index.
        graph = self._graph
        if graph is None:
            return
        locations = self._factory.definition_processor.go_type_locations
        try:
            rows = self._owned_rows(
                graph.fetch_all(
                    cs.CYPHER_ALL_GO_TYPE_LOCATIONS,
                    {cs.KEY_PROJECT_PREFIX: self._project_name + "."},
                ),
                cs.KEY_QUALIFIED_NAME,
            )
        except Exception:
            if not run.is_full_build:
                raise
            logger.warning(ls.REHYDRATE_QUERY_FAILED)
            return
        restored = 0
        for row in rows:
            entry = _go_type_location(row)
            if entry is None:
                continue
            key, located = entry
            if key not in locations:
                locations[key] = located
                restored += 1
        if restored:
            logger.info(ls.GO_TYPE_LOCATIONS_REHYDRATED.format(count=restored))

    def rehydrate_function_locations(self, run: RehydrationRun) -> None:
        # The C#/Go semantic call + LINQ joins resolve targets against
        # function_locations, keyed (module_qn, line, col) at the definition
        # START; Go's join additionally keys at the NAME token, whose column
        # is persisted separately (issue #1240). Fresh Pass-2 entries win;
        # rehydrated records serve the joins only (Pass 3 touches re-parsed
        # files exclusively), so a METHOD's container comes from the query
        # and is_named stays True (generated qns never persist under Module
        # DEFINES).
        graph = self._graph
        if graph is None:
            return
        locations = self._factory.facts.function_locations
        params = {cs.KEY_PROJECT_PREFIX: self._project_name + "."}
        restored = 0
        for query in (
            cs.CYPHER_ALL_FUNCTION_LOCATIONS,
            cs.CYPHER_ALL_METHOD_LOCATIONS,
        ):
            try:
                rows = self._owned_rows(
                    graph.fetch_all(query, params), cs.KEY_QUALIFIED_NAME
                )
            except Exception:
                if not run.is_full_build:
                    raise
                logger.warning(ls.REHYDRATE_QUERY_FAILED)
                return
            restored += self._restore_function_rows(run, rows, locations)
        if restored:
            logger.info(ls.FUNCTION_LOCATIONS_REHYDRATED.format(count=restored))

    def _restore_function_rows(
        self, run: RehydrationRun, rows: list[ResultRow], locations: FunctionLocations
    ) -> int:
        restored = 0
        for row in rows:
            module_qn = self._declaring_module_qn(run, row)
            qn = row.get(cs.KEY_QUALIFIED_NAME)
            label = row.get(cs.KEY_LABEL)
            container = row.get(cs.KEY_CONTAINER_QN)
            start_line = _persisted_int(row.get(cs.KEY_START_LINE))
            start_col = _persisted_int(row.get(cs.KEY_START_COL))
            name_line = _persisted_int(row.get(cs.KEY_NAME_START_LINE))
            name_col = _persisted_int(row.get(cs.KEY_NAME_START_COL))
            if not (
                isinstance(module_qn, str)
                and isinstance(qn, str)
                and isinstance(label, str)
                and start_line is not None
                and start_col is not None
            ):
                continue
            record = FunctionLocation(
                label=label,
                qualified_name=qn,
                container_qn=container if isinstance(container, str) else None,
            )
            keys = {(module_qn, start_line, start_col)}
            if name_col is not None:
                # The NAME token can sit on a LATER line than the declaration
                # start (a multiline Go receiver), so the alias keys at its
                # own persisted line, never the declaration's.
                keys.add((module_qn, name_line or start_line, name_col))
            for key in keys:
                if key not in locations:
                    locations[key] = record
                    restored += 1
        return restored

    def _declaring_module_qn(self, run: RehydrationRun, row: ResultRow) -> object:
        # Pass 2 keys a definition by the file declaring it. A Go method in
        # another file than its receiver type hangs off the type's Module, so
        # the row's module named the wrong file: two files' methods at one
        # line and column collided on one key, and the one left out was
        # owned by no file and outlived its file's re-parse (issue #2483).
        path = row.get(cs.KEY_PATH)
        if isinstance(path, str) and path.endswith(cs.EXT_GO):
            return self.recorded_module_qn(path, run)
        return row.get(cs.KEY_MODULE_QN)
