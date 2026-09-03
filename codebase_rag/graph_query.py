"""Deterministic graph queries for agents and the `cgr graph` commands.

`query_code_graph` turns natural language into Cypher through an LLM, which
is the wrong shape for "go to definition" or "find callers": those must be
exact and repeatable. Everything here is fixed Cypher scoped to one project
plus client-side walks over the fetched rows, with every result list
sorted, so the same graph always yields the same JSON (issue #1523).

`callers` and `callees` return one row per call SITE: the CALLS edges carry
the site location from issue #1522, so an agent can jump to, check, or
rewrite each call. Edges written without a site (libclang macro uses,
Roslyn facts, trace write-back) return `null` positions.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TypedDict

from . import constants as cs
from . import cypher_queries as cq
from .dead_code import (
    _is_test_symbol,
    _node_props,
    _NodeId,
    _rust_test_fn_spans,
    _rust_test_modules_from_nodes,
)
from .types_defs import PropertyDict, ResultRow
from .utils.source_extraction import extract_source_lines

QueryFn = Callable[[str, PropertyDict | None], list[ResultRow]]

_DEFINITION_LABELS = frozenset(
    {
        cs.NodeLabel.FUNCTION.value,
        cs.NodeLabel.METHOD.value,
        cs.NodeLabel.CLASS.value,
        cs.NodeLabel.INTERFACE.value,
        cs.NodeLabel.ENUM.value,
        cs.NodeLabel.TYPE.value,
        cs.NodeLabel.UNION.value,
        cs.NodeLabel.MODULE.value,
    }
)
_REACH_RELS = frozenset(
    {
        cs.RelationshipType.CALLS.value,
        cs.RelationshipType.REFERENCES.value,
        cs.RelationshipType.INSTANTIATES.value,
    }
)


class SymbolRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    start_line: int | None
    end_line: int | None


class DefinitionRow(SymbolRow):
    name: str | None
    docstring: str | None
    source: str | None
    found: bool


class CallSiteRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    line: int | None
    col: int | None
    end_line: int | None
    end_col: int | None
    arg_count: int | None
    kwarg_names: list[str] | None
    resolution: str | None
    depth: int
    through: str


class RelatedRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    relationship: str


class ImporterRow(TypedDict):
    module: str
    path: str | None
    line: int | None
    col: int | None
    end_line: int | None
    end_col: int | None
    alias: str | None
    imported_name: str | None


class TestReachRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    depth: int
    through: str


def _prefix(project_name: str) -> str:
    return f"{project_name}{cs.SEPARATOR_DOT}"


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _symbol_row(row: ResultRow) -> SymbolRow:
    return SymbolRow(
        label=str(row.get(cs.KEY_LABEL, "")),
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        path=_opt_str(row.get(cs.KEY_PATH)),
        start_line=_opt_int(row.get(cs.KEY_START_LINE)),
        end_line=_opt_int(row.get(cs.KEY_END_LINE)),
    )


def _symbol_key(row: SymbolRow) -> tuple[str, str]:
    return (row["qualified_name"], row["path"] or "")


# --- resolve ------------------------------------------------------------------


def resolve(fetch_all: QueryFn, project_name: str, target: str) -> list[SymbolRow]:
    """Definitions a name or `path:line` refers to, exact first, then suffix.

    `target` is a qualified name, a bare name (`helper`, `Store.get`), or
    `path:line` (repo-relative path and 1-based line). Names match on the
    node's `name` or as a dotted suffix of its qualified name; a location
    returns the innermost definitions spanning that line.
    """
    prefix = _prefix(project_name)
    path, sep, line_text = target.rpartition(cs.CHAR_COLON)
    if sep and line_text.isdigit() and path:
        rows = fetch_all(
            cq.CYPHER_GRAPH_RESOLVE_LOCATION,
            {
                cs.KEY_PROJECT_PREFIX: prefix,
                cs.KEY_PATH: path,
                cs.KEY_LINE: int(line_text),
            },
        )
        symbols = [_symbol_row(r) for r in rows]
        # Innermost first: the tightest span is what the line "is in".
        symbols.sort(
            key=lambda s: (
                (s["end_line"] or 0) - (s["start_line"] or 0),
                s["qualified_name"],
            )
        )
        return symbols
    rows = fetch_all(
        cq.CYPHER_GRAPH_RESOLVE_NAME,
        {
            cs.KEY_PROJECT_PREFIX: prefix,
            cs.KEY_NAME: target.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            cs.KEY_SUFFIX: f"{cs.SEPARATOR_DOT}{target}",
            cs.KEY_QN: target,
        },
    )
    symbols = [_symbol_row(r) for r in rows]
    exact = [s for s in symbols if s["qualified_name"] == target]
    suffix = [
        s
        for s in symbols
        if s["qualified_name"] != target
        and s["qualified_name"].endswith(f"{cs.SEPARATOR_DOT}{target}")
    ]
    by_name = [s for s in symbols if s not in exact and s not in suffix]
    ordered: list[SymbolRow] = []
    for bucket in (exact, suffix, by_name):
        ordered.extend(sorted(bucket, key=_symbol_key))
    return ordered


# --- definition ---------------------------------------------------------------


def source_root_for(
    fetch_all: QueryFn, project_name: str, repo_root: Path
) -> Path | None:
    """`repo_root` when the graph project was indexed from it, else None.

    A definition of another project carries a relative path that may also
    exist under `repo_root`; reading it there would return the wrong file. A
    matching project name is not enough either: the Project node's stored
    root path must be this repository.
    """
    rows = fetch_all(
        cq.CYPHER_PROJECT_ROOT_PATH,
        {
            cs.KEY_PROJECT_NAME: project_name,
            cs.KEY_PROJECT_PREFIX: _prefix(project_name),
        },
    )
    stored = _opt_str(rows[0].get(cs.KEY_ROOT_PATH)) if rows else None
    if not stored:
        return None
    local = repo_root.resolve()
    return local if Path(stored).resolve() == local else None


def definition(
    fetch_all: QueryFn, project_name: str, qualified_name: str, repo_root: Path | None
) -> DefinitionRow:
    """File, span, docstring and source of one definition.

    Source is read from `repo_root` when the node's repo-relative path stays
    inside it; a graph indexed elsewhere still answers with the span.
    """
    rows = fetch_all(
        cq.CYPHER_GRAPH_DEFINITION,
        {cs.KEY_PROJECT_PREFIX: _prefix(project_name), cs.KEY_QN: qualified_name},
    )
    if not rows:
        return DefinitionRow(
            label="",
            qualified_name=qualified_name,
            path=None,
            start_line=None,
            end_line=None,
            name=None,
            docstring=None,
            source=None,
            found=False,
        )
    row = rows[0]
    symbol = _symbol_row(row)
    source: str | None = None
    path, start, end = symbol["path"], symbol["start_line"], symbol["end_line"]
    if repo_root is not None and path and start and end:
        candidate = (repo_root / path).resolve()
        if candidate.is_relative_to(repo_root.resolve()) and candidate.is_file():
            source = extract_source_lines(candidate, start, end)
    return DefinitionRow(
        label=symbol["label"],
        qualified_name=symbol["qualified_name"],
        path=path,
        start_line=start,
        end_line=end,
        name=_opt_str(row.get(cs.KEY_NAME)),
        docstring=_opt_str(row.get(cs.KEY_DOCSTRING)),
        source=source,
        found=True,
    )


# --- callers / callees --------------------------------------------------------


def _site_row(row: ResultRow, depth: int, through: str) -> CallSiteRow:
    kwargs = row.get(cs.KEY_KWARG_NAMES)
    return CallSiteRow(
        label=str(row.get(cs.KEY_LABEL, "")),
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME, "")),
        path=_opt_str(row.get(cs.KEY_PATH)),
        line=_opt_int(row.get(cs.KEY_LINE)),
        col=_opt_int(row.get(cs.KEY_COL)),
        end_line=_opt_int(row.get(cs.KEY_END_LINE)),
        end_col=_opt_int(row.get(cs.KEY_END_COL)),
        arg_count=_opt_int(row.get(cs.KEY_ARG_COUNT)),
        kwarg_names=[str(k) for k in kwargs] if isinstance(kwargs, list) else None,
        resolution=_opt_str(row.get(cs.KEY_RESOLUTION)),
        depth=depth,
        through=through,
    )


def _site_sort_key(row: CallSiteRow) -> tuple[int, str, str, int, int]:
    return (
        row["depth"],
        row["through"],
        row["qualified_name"],
        row["line"] if row["line"] is not None else -1,
        row["col"] if row["col"] is not None else -1,
    )


def _walk_sites(
    fetch_all: QueryFn, project_name: str, query: str, start: str, depth: int
) -> list[CallSiteRow]:
    # Breadth-first over endpoints, one query per frontier node; a node's
    # sites appear at the depth it was first reached and never again, so a
    # cycle terminates and the output stays a finite, ordered list.
    prefix = _prefix(project_name)
    seen: set[str] = {start}
    frontier: list[str] = [start]
    out: list[CallSiteRow] = []
    for level in range(1, max(1, depth) + 1):
        next_frontier: list[str] = []
        for qn in sorted(frontier):
            rows = fetch_all(query, {cs.KEY_PROJECT_PREFIX: prefix, cs.KEY_QN: qn})
            for row in rows:
                site = _site_row(row, level, qn)
                out.append(site)
                other = site["qualified_name"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier:
            break
    return sorted(out, key=_site_sort_key)


def callers(
    fetch_all: QueryFn, project_name: str, qualified_name: str, depth: int = 1
) -> list[CallSiteRow]:
    """Call sites that reach `qualified_name`, one row per site.

    `depth` > 1 follows the callers' callers; `through` names the callee
    each row's site invokes, so a transitive row is still one exact site.
    """
    return _walk_sites(
        fetch_all, project_name, cq.CYPHER_GRAPH_CALLERS, qualified_name, depth
    )


def callees(
    fetch_all: QueryFn, project_name: str, qualified_name: str, depth: int = 1
) -> list[CallSiteRow]:
    """Call sites inside `qualified_name`, one row per site (`through` = caller)."""
    return _walk_sites(
        fetch_all, project_name, cq.CYPHER_GRAPH_CALLEES, qualified_name, depth
    )


# --- implementors / overrides / importers ---------------------------------------


def _related_rows(
    fetch_all: QueryFn, project_name: str, query: str, qn: str
) -> list[RelatedRow]:
    rows = fetch_all(
        query, {cs.KEY_PROJECT_PREFIX: _prefix(project_name), cs.KEY_QN: qn}
    )
    out = [
        RelatedRow(
            label=str(r.get(cs.KEY_LABEL, "")),
            qualified_name=str(r.get(cs.KEY_QUALIFIED_NAME, "")),
            path=_opt_str(r.get(cs.KEY_PATH)),
            relationship=str(r.get(cs.KEY_REL_TYPE, "")),
        )
        for r in rows
    ]
    return sorted(out, key=lambda r: (r["qualified_name"], r["relationship"]))


def implementors(
    fetch_all: QueryFn, project_name: str, qualified_name: str
) -> list[RelatedRow]:
    """Types that INHERIT from or IMPLEMENT `qualified_name`."""
    return _related_rows(
        fetch_all, project_name, cq.CYPHER_GRAPH_IMPLEMENTORS, qualified_name
    )


def overrides(
    fetch_all: QueryFn, project_name: str, qualified_name: str
) -> list[RelatedRow]:
    """Methods that OVERRIDE `qualified_name`, and the method it overrides."""
    return _related_rows(
        fetch_all, project_name, cq.CYPHER_GRAPH_OVERRIDES, qualified_name
    )


def importers(
    fetch_all: QueryFn, project_name: str, module_qn: str
) -> list[ImporterRow]:
    """Modules importing `module_qn`, with each import statement's location."""
    rows = fetch_all(
        cq.CYPHER_GRAPH_IMPORTERS,
        {cs.KEY_PROJECT_PREFIX: _prefix(project_name), cs.KEY_QN: module_qn},
    )
    out = [
        ImporterRow(
            module=str(r.get(cs.KEY_QUALIFIED_NAME, "")),
            path=_opt_str(r.get(cs.KEY_PATH)),
            line=_opt_int(r.get(cs.KEY_LINE)),
            col=_opt_int(r.get(cs.KEY_COL)),
            end_line=_opt_int(r.get(cs.KEY_END_LINE)),
            end_col=_opt_int(r.get(cs.KEY_END_COL)),
            alias=_opt_str(r.get(cs.KEY_ALIAS)),
            imported_name=_opt_str(r.get(cs.KEY_IMPORTED_NAME)),
        )
        for r in rows
    ]
    return sorted(
        out,
        key=lambda r: (
            r["module"],
            r["line"] if r["line"] is not None else -1,
            # `or -1` would fold a real column 0 -- the common case, an import
            # at the start of a line -- into the same key as a missing column,
            # leaving co-located rows in the arbitrary order the graph
            # returned them.
            r["col"] if r["col"] is not None else -1,
            r["alias"] or "",
            r["imported_name"] or "",
        ),
    )


# --- tests_reaching ------------------------------------------------------------


class ReachIndex:
    """The project's reverse call graph plus the test classifier's inputs.

    Built once from the dead-code fetch (one query each for nodes and edges)
    so a caller with several symbols to look up (a structural delta, issue
    #1525) does not re-read the project per symbol.
    """

    def __init__(
        self,
        nodes: dict[_NodeId, PropertyDict],
        reverse: dict[str, set[str]],
        test_patterns: tuple[str, ...],
    ) -> None:
        self._by_qn: dict[str, tuple[str, PropertyDict]] = {
            str(qn): (label, props) for (label, qn), props in nodes.items()
        }
        self._reverse = reverse
        self._patterns = test_patterns
        self._rust_modules = _rust_test_modules_from_nodes(nodes)
        self._rust_spans = _rust_test_fn_spans(nodes)

    @classmethod
    def build(
        cls,
        fetch_all: QueryFn,
        project_name: str,
        test_patterns: tuple[str, ...] = cs.TEST_PATH_PATTERNS,
    ) -> ReachIndex:
        params = {cs.KEY_PROJECT_PREFIX: _prefix(project_name)}
        nodes: dict[_NodeId, PropertyDict] = {}
        for row in fetch_all(cq.CYPHER_DEAD_CODE_NODES, params):
            qn = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
            if qn:
                nodes[(str(row.get(cs.KEY_LABEL, "")), qn)] = _node_props(row)
        reverse: dict[str, set[str]] = {}
        for row in fetch_all(cq.CYPHER_DEAD_CODE_RELS, params):
            if str(row.get(cs.KEY_REL_TYPE, "")) not in _REACH_RELS:
                continue
            src = str(row.get(cs.KEY_FROM_QN) or "")
            dst = str(row.get(cs.KEY_TO_QN) or "")
            if src and dst:
                reverse.setdefault(dst, set()).add(src)
        return cls(nodes, reverse, test_patterns)

    def _walk(self, qualified_name: str) -> tuple[dict[str, int], dict[str, str]]:
        depth_of: dict[str, int] = {qualified_name: 0}
        through_of: dict[str, str] = {qualified_name: qualified_name}
        frontier = [qualified_name]
        while frontier:
            next_frontier: list[str] = []
            for qn in sorted(frontier):
                for caller in sorted(self._reverse.get(qn, ())):
                    if caller in depth_of:
                        continue
                    depth_of[caller] = depth_of[qn] + 1
                    through_of[caller] = qn
                    next_frontier.append(caller)
            frontier = next_frontier
        return depth_of, through_of

    def tests_reaching(self, qualified_name: str) -> list[TestReachRow]:
        depth_of, through_of = self._walk(qualified_name)
        out: list[TestReachRow] = []
        for qn, depth in depth_of.items():
            if qn == qualified_name:
                continue
            entry = self._by_qn.get(qn)
            if entry is None:
                continue
            label, props = entry
            path = str(props.get(cs.KEY_PATH) or "")
            if _is_test_symbol(
                props, qn, path, self._patterns, self._rust_modules, self._rust_spans
            ):
                out.append(
                    TestReachRow(
                        label=label,
                        qualified_name=qn,
                        path=path or None,
                        depth=depth,
                        through=through_of[qn],
                    )
                )
        return sorted(out, key=lambda r: (r["depth"], r["qualified_name"]))


def tests_reaching(
    fetch_all: QueryFn,
    project_name: str,
    qualified_name: str,
    test_patterns: tuple[str, ...] = cs.TEST_PATH_PATTERNS,
) -> list[TestReachRow]:
    """Test symbols from which `qualified_name` is reachable, with distance.

    Walks CALLS / REFERENCES / INSTANTIATES backwards over the project's
    edges (the dead-code fetch, one query each for nodes and edges) and keeps
    the reached definitions the dead-code root classifier calls tests, so
    Rust `#[cfg(test)]` modules count exactly as they do there.
    """
    return ReachIndex.build(fetch_all, project_name, test_patterns).tests_reaching(
        qualified_name
    )
