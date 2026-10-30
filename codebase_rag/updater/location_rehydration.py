"""Read back the location indexes the semantic joins resolve against, for the
files an incremental sync did not re-parse (issues #1229, #1240)."""

from collections.abc import Callable

from loguru import logger

from .. import constants as cs
from .. import logs as ls
from ..parsers.factory import ProcessorFactory
from ..services import QueryProtocol
from ..types_defs import FunctionLocation, FunctionLocations, ResultRow
from .rehydration import RehydrationRun


def _persisted_int(value: object) -> int | None:
    # bool is an int subclass; a stray True would key as 1 and shadow a real
    # entry, so it is rejected explicitly (same discipline as the C# path).
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


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


class LocationRehydrator:
    """The (path, line[, col]) indexes Pass 2 fills only for re-parsed files.

    Fresh Pass-2 entries win over what is read back. `owned_rows` keeps the
    rows whose qualified name this project owns, and `recorded_module_qn`
    names the module the graph records for a path.
    """

    def __init__(
        self,
        graph: QueryProtocol | None,
        factory: ProcessorFactory,
        project_name: str,
        owned_rows: Callable[[list[ResultRow], str], list[ResultRow]],
        recorded_module_qn: Callable[[str, RehydrationRun], str],
    ) -> None:
        self._graph = graph
        self._factory = factory
        self._project_name = project_name
        self._owned_rows = owned_rows
        self._recorded_module_qn = recorded_module_qn

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
            return self._recorded_module_qn(path, run)
        return row.get(cs.KEY_MODULE_QN)
