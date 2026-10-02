"""Deterministic graph queries for agents and the `cgr graph` commands.

`query_code_graph` turns natural language into Cypher through an LLM, which
is the wrong shape for "go to definition" or "find callers": those must be
exact and repeatable. Everything here is fixed Cypher scoped to one project
plus client-side walks over the fetched rows, with every result list
sorted, so the same graph always yields the same JSON (issue #1523).

`callers` and `callees` return one row per call SITE: the CALLS edges carry
the site location from issue #1522, so an agent can jump to, check, or
rewrite each call. Edges written without a site (libclang macro uses,
Roslyn facts, trace write-back) return `null` positions. In both directions
`path` is the file the site sits in (its caller's), so `path:line` is always
the call; `callee_path` is where the invoked symbol is defined (issue #2460).
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import NamedTuple, TypedDict

from . import constants as cs
from . import cypher_queries as cq
from .dead_code import (
    _is_test_symbol,
    _node_props,
    _NodeId,
    _rust_test_fn_spans,
    _rust_test_modules_from_nodes,
)
from .types_defs import PropertyDict, PropertyParams, ResultRow
from .utils.source_extraction import extract_source_lines

QueryFn = Callable[[str, PropertyParams | None], list[ResultRow]]

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


class EndpointRow(TypedDict):
    endpoint: str
    kind: str | None
    label: str
    handler: str
    path: str | None
    callers: int


class EndpointCallerRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    url: str | None
    direction: str | None
    endpoint: str
    handler: str


class RemoteDependencyRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    url: str | None
    direction: str | None
    endpoint: str | None
    handler: str | None
    handler_project: str | None


class CallSiteRow(TypedDict):
    label: str
    qualified_name: str
    path: str | None
    callee_path: str | None
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


def _owner_check(fetch_all: QueryFn, project_name: str) -> Callable[[str], bool]:
    """Whether `project_name`, and not a longer-named registered project it
    is a dotted prefix of, owns a qualified name (issue #1982).

    Every read here is scoped by `STARTS WITH $project_prefix`, and `foo.`
    selects `foo.bar`'s rows too. The owner is the LONGEST registered
    project name the qn sits under, the way the updater and the gloss
    repair decide it; the project list is read once per call, and a failed
    read keeps the prefix rule rather than dropping every row.
    """
    prefix = _prefix(project_name)
    names: set[str] = {project_name}
    try:
        rows = fetch_all(cq.CYPHER_LIST_PROJECTS, None)
    except Exception:
        rows = []
    for row in rows:
        name = row.get(cs.KEY_NAME)
        if isinstance(name, str) and name:
            names.add(name)
    longest_first = sorted(names, key=len, reverse=True)

    def owns(qn: str) -> bool:
        if qn != project_name and not qn.startswith(prefix):
            return False
        for name in longest_first:
            if qn == name or qn.startswith(f"{name}{cs.SEPARATOR_DOT}"):
                return name == project_name
        return True

    return owns


def _prefix(project_name: str) -> str:
    return f"{project_name}{cs.SEPARATOR_DOT}"


def _text_qn(row: ResultRow) -> str:
    return str(row.get(cs.KEY_QUALIFIED_NAME, ""))


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


class TargetRefusal(TypedDict):
    error: str


class Location(NamedTuple):
    path: str
    line: int


def parse_location(target: str) -> Location | None:
    """`(path, line)` when `target` is a `path:line` location, else None.

    One parser for every tool that accepts a resolve target, so "is this a
    location" is answered the same way by `resolve` and by anything that
    decides differently for a location (a gloss anchors to the innermost
    definition spanning a line, and refuses an ambiguous NAME).
    `path:line:col`, the form compilers, linters and `grep -n` print, is a
    location too; the column cannot change which definitions span the line
    (issue #2611).
    """
    readings = _location_readings(target)
    return readings[0] if readings else None


def _location_readings(target: str) -> list[Location]:
    """Every way `target` reads as a location, the likeliest first.

    A trailing `:<digits>` is the line. When what precedes it also ends in
    `:<digits>` that is a line and a column, but a POSIX file name may
    itself end in `:<digits>`, so the longer path is kept as a second
    reading for `resolve` to try. Any other colon (a Windows drive, one
    inside a directory name) stays part of the path.
    """
    head, sep, last = target.rpartition(cs.CHAR_COLON)
    if not (sep and head and last.isdecimal()):
        return []
    whole = Location(head, int(last))
    path, sep, line = head.rpartition(cs.CHAR_COLON)
    if sep and path and line.isdecimal():
        return [Location(path, int(line)), whole]
    return [whole]


def _posix_spelling(path: str) -> str:
    # Nodes store `as_posix()` of the path relative to the root, so `\`
    # separators, `./` and doubled slashes are spellings of the same file.
    return posixpath.normpath(path.replace(cs.SEPARATOR_BACKSLASH, cs.SEPARATOR_SLASH))


def _is_absolute(path: str) -> bool:
    # A Windows path is absolute wherever the graph is read: the root the
    # project was indexed under may be one.
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _spellings(path: str) -> list[str]:
    """`path` as written and, when native here, with its symlinks resolved:
    the stored root is resolved at index time, a caller's path may not be
    (`$PWD` through a symlink, `/tmp` on macOS)."""
    forms = [_posix_spelling(path)]
    native = Path(path)
    if native.is_absolute():
        try:
            forms.append(native.resolve().as_posix())
        except (OSError, RuntimeError):
            pass
    return list(dict.fromkeys(forms))


def _relative_under(path: str, root: str) -> str | None:
    base = root.rstrip(cs.SEPARATOR_SLASH) + cs.SEPARATOR_SLASH
    head, rest = path[: len(base)], path[len(base) :]
    # A Windows drive compares case-insensitively: an editor reports `c:\`
    # for the `C:\` the root was stored under.
    if PureWindowsPath(base).drive:
        head, base = head.casefold(), base.casefold()
    return rest if head == base and rest else None


def _location_paths(path: str, roots: Sequence[str]) -> list[str]:
    """The stored, repo-relative paths `path` may name, likeliest first.

    An absolute path inside a root is made relative to it. One inside no
    root is kept as written: it matches nothing, and a refusal names it
    rather than guessing at a suffix that may be another checkout's file.
    """
    posix = _posix_spelling(path)
    if not _is_absolute(posix):
        return [posix]
    root_forms = [form for root in roots if root for form in _spellings(root)]
    relative = [
        rest
        for form in _spellings(posix)
        for root in root_forms
        if (rest := _relative_under(form, root)) is not None
    ]
    return list(dict.fromkeys(relative)) or [posix]


def _stored_root(fetch_all: QueryFn, project_name: str) -> str | None:
    """The root the project was indexed from, as its Project node holds it."""
    rows = fetch_all(
        cq.CYPHER_PROJECT_ROOT_PATH,
        {
            cs.KEY_PROJECT_NAME: project_name,
            cs.KEY_PROJECT_PREFIX: _prefix(project_name),
        },
    )
    return _opt_str(rows[0].get(cs.KEY_ROOT_PATH)) if rows else None


def _location_roots(fetch_all: QueryFn, project_name: str) -> list[str]:
    """Where an absolute location path is made relative from: only the root
    the project was indexed from. A caller's checkout is not trusted for it:
    an unrelated checkout holding the same relative file would turn its path
    into this project's symbol, and `annotate` would write on it (bot
    review). The project's own root reached through a symlink still
    matches, since `_spellings` resolves the target as well as the root."""
    stored = _stored_root(fetch_all, project_name)
    return [stored] if stored else []


# --- resolve ------------------------------------------------------------------


def resolve(fetch_all: QueryFn, project_name: str, target: str) -> list[SymbolRow]:
    """Definitions a name or `path:line` refers to, exact first, then suffix.

    `target` is a qualified name, a bare name (`helper`, `Store.get`), or
    `path:line[:col]` (1-based line; the path relative to the repository
    root, or absolute inside the root the project was indexed from). Names
    match on the node's `name` or as a dotted suffix of its qualified name;
    a location returns the innermost definitions spanning that line, and
    nothing when `resolve_or_refuse` would refuse it.
    """
    prefix = _prefix(project_name)
    owns = _owner_check(fetch_all, project_name)
    if parse_location(target) is not None:
        found = _resolve_location(fetch_all, project_name, target, owns)
        return found if isinstance(found, list) else []
    rows = fetch_all(
        cq.CYPHER_GRAPH_RESOLVE_NAME,
        {
            cs.KEY_PROJECT_PREFIX: prefix,
            cs.KEY_NAME: target.rsplit(cs.SEPARATOR_DOT, 1)[-1],
            cs.KEY_SUFFIX: f"{cs.SEPARATOR_DOT}{target}",
            cs.KEY_QN: target,
        },
    )
    owned = [r for r in rows if owns(_text_qn(r))]
    symbols = [_symbol_row(r) for r in owned]
    # A C# type reached by `<namespace>.<name>` ranks with the dotted-suffix
    # matches: that is the name its qualified name used to end with before
    # the mirrored namespace was left out of it (issue #1629).
    natural = {
        str(r.get(cs.KEY_QUALIFIED_NAME, ""))
        for r in owned
        if r.get(cs.KEY_NAMESPACE)
        and f"{r[cs.KEY_NAMESPACE]}{cs.SEPARATOR_DOT}{r.get(cs.KEY_NAME)}" == target
    }
    exact = [s for s in symbols if s["qualified_name"] == target]
    suffix = [
        s
        for s in symbols
        if s["qualified_name"] != target
        and (
            s["qualified_name"].endswith(f"{cs.SEPARATOR_DOT}{target}")
            or s["qualified_name"] in natural
        )
    ]
    by_name = [s for s in symbols if s not in exact and s not in suffix]
    ordered: list[SymbolRow] = []
    for bucket in (exact, suffix, by_name):
        ordered.extend(sorted(bucket, key=_symbol_key))
    return ordered


def resolve_or_refuse(
    fetch_all: QueryFn, project_name: str, target: str
) -> list[SymbolRow] | TargetRefusal:
    """`resolve`, or a refusal when `target` is a location the project
    cannot answer for: a file it does not hold, or two held files the target
    could equally name (issue #2611). `cgr graph resolve` and the MCP
    `resolve`, `annotate` and `glosses` tools all answer through this, so a
    target is refused, or not, the same way everywhere."""
    if parse_location(target) is None:
        return resolve(fetch_all, project_name, target)
    owns = _owner_check(fetch_all, project_name)
    return _resolve_location(fetch_all, project_name, target, owns)


def _held_paths(
    fetch_all: QueryFn,
    project_name: str,
    paths: Sequence[str],
    owns: Callable[[str], bool],
) -> set[str]:
    rows = fetch_all(
        cq.CYPHER_GRAPH_LOCATION_FILES,
        {
            cs.KEY_PROJECT_PREFIX: _prefix(project_name),
            cs.CYPHER_PARAM_PATHS: sorted(set(paths)),
        },
    )
    return {
        path
        for row in rows
        if owns(_text_qn(row)) and (path := _opt_str(row.get(cs.KEY_PATH)))
    }


def _definitions_at(
    fetch_all: QueryFn,
    project_name: str,
    location: Location,
    owns: Callable[[str], bool],
) -> list[SymbolRow]:
    rows = fetch_all(
        cq.CYPHER_GRAPH_RESOLVE_LOCATION,
        {
            cs.KEY_PROJECT_PREFIX: _prefix(project_name),
            cs.KEY_PATH: location.path,
            cs.KEY_LINE: location.line,
        },
    )
    symbols = [_symbol_row(r) for r in rows if owns(_text_qn(r))]
    # Innermost first: the tightest span is what the line "is in".
    symbols.sort(
        key=lambda s: (
            (s["end_line"] or 0) - (s["start_line"] or 0),
            s["qualified_name"],
        )
    )
    return symbols


def _resolve_location(
    fetch_all: QueryFn,
    project_name: str,
    target: str,
    owns: Callable[[str], bool],
) -> list[SymbolRow] | TargetRefusal:
    readings = _location_readings(target)
    absolute = any(_is_absolute(_posix_spelling(r.path)) for r in readings)
    roots = _location_roots(fetch_all, project_name) if absolute else []
    spelled = {r: _location_paths(r.path, roots) for r in readings}
    every_path = [path for paths in spelled.values() for path in paths]
    held: set[str] | None = None
    if len(readings) > 1:
        # `x:a:b` is line a of `x` or line b of `x:a`. Only a reading whose
        # file the project holds is tried, and when it holds both neither
        # is: falling through to the second reading when the first line has
        # no definition would answer from the other file (bot review).
        held = _held_paths(fetch_all, project_name, every_path, owns)
        holding = [r for r in readings if held.intersection(spelled[r])]
        if len(holding) > 1:
            first, second = (
                Location(next(p for p in spelled[r] if p in held), r.line)
                for r in holding
            )
            return TargetRefusal(
                error=cs.GRAPH_LOCATION_AMBIGUOUS.format(
                    target=target,
                    project=project_name,
                    path=first.path,
                    line=first.line,
                    other_path=second.path,
                    other_line=second.line,
                )
            )
        spelled = {r: spelled[r] for r in holding}
    # The spellings of one reading are of one file: the first that names
    # definitions wins.
    for reading, paths in spelled.items():
        for path in paths:
            if symbols := _definitions_at(
                fetch_all, project_name, Location(path, reading.line), owns
            ):
                return symbols
    # Only an empty answer pays for the file lookup when it was not needed
    # above: `[]` for a held file means no definition spans the line.
    if held is None:
        held = _held_paths(fetch_all, project_name, every_path, owns)
    if held:
        return []
    return TargetRefusal(
        error=cs.GRAPH_LOCATION_UNKNOWN_FILE.format(
            path=every_path[0], project=project_name
        )
    )


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
    stored = _stored_root(fetch_all, project_name)
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
    owns = _owner_check(fetch_all, project_name)
    rows = [r for r in rows if owns(_text_qn(r))]
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
        callee_path=_opt_str(row.get(cs.KEY_CALLEE_PATH)),
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
    owns = _owner_check(fetch_all, project_name)
    seen: set[str] = {start}
    frontier: list[str] = [start]
    out: list[CallSiteRow] = []
    for level in range(1, max(1, depth) + 1):
        next_frontier: list[str] = []
        for qn in sorted(frontier):
            rows = fetch_all(query, {cs.KEY_PROJECT_PREFIX: prefix, cs.KEY_QN: qn})
            for site in _owned_sites(rows, owns, level, qn):
                out.append(site)
                other = site["qualified_name"]
                if other not in seen:
                    seen.add(other)
                    next_frontier.append(other)
        frontier = next_frontier
        if not frontier:
            break
    return sorted(out, key=_site_sort_key)


def _owned_sites(
    rows: list[ResultRow], owns: Callable[[str], bool], level: int, through: str
) -> list[CallSiteRow]:
    """The sites among `rows` whose other endpoint this project owns: a
    foreign row is neither reported nor a hop the walk continues through."""
    return [_site_row(row, level, through) for row in rows if owns(_text_qn(row))]


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
    """Call sites inside `qualified_name`, one row per site (`through` = caller).

    `path` is `through`'s file, where the site is, not the callee's: that
    one is `callee_path`, so a hop past depth 1 still reads as `path:line`.
    """
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
    owns = _owner_check(fetch_all, project_name)
    rows = [r for r in rows if owns(_text_qn(r))]
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
    owns = _owner_check(fetch_all, project_name)
    rows = [r for r in rows if owns(_text_qn(r))]
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
    and walked backwards from one symbol; the structural delta (issue #1525)
    walks callers per hop instead and does not use it.
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
        owns = _owner_check(fetch_all, project_name)
        for row in fetch_all(cq.CYPHER_DEAD_CODE_NODES, params):
            qn = str(row.get(cs.KEY_QUALIFIED_NAME) or "")
            if not owns(qn):
                continue
            if qn:
                nodes[(str(row.get(cs.KEY_LABEL, "")), qn)] = _node_props(row)
        reverse: dict[str, set[str]] = {}
        for row in fetch_all(cq.CYPHER_DEAD_CODE_RELS, params):
            if str(row.get(cs.KEY_REL_TYPE, "")) not in _REACH_RELS:
                continue
            src = str(row.get(cs.KEY_FROM_QN) or "")
            dst = str(row.get(cs.KEY_TO_QN) or "")
            # Both ends owned: a foreign caller must neither be reported
            # nor be a hop the walk continues through (issue #1982).
            if src and dst and owns(src) and owns(dst):
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


# --- cross-service edges (issue #1603) ---------------------------------------


def _endpoint_row(row: ResultRow) -> EndpointRow:
    callers = row.get(cs.KEY_CALLERS)
    return EndpointRow(
        endpoint=str(row.get(cs.KEY_ENDPOINT) or ""),
        kind=_opt_str(row.get(cs.KEY_KIND)),
        label=str(row.get(cs.KEY_LABEL) or ""),
        handler=str(row.get(cs.KEY_HANDLER) or ""),
        path=_opt_str(row.get(cs.KEY_PATH)),
        callers=callers if isinstance(callers, int) else 0,
    )


def _endpoint_caller_row(row: ResultRow) -> EndpointCallerRow:
    return EndpointCallerRow(
        label=str(row.get(cs.KEY_LABEL) or ""),
        qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME) or ""),
        path=_opt_str(row.get(cs.KEY_PATH)),
        url=_opt_str(row.get(cs.KEY_URL)),
        direction=_opt_str(row.get(cs.KEY_DIRECTION)),
        endpoint=str(row.get(cs.KEY_ENDPOINT) or ""),
        handler=str(row.get(cs.KEY_HANDLER) or ""),
    )


def endpoints(fetch_all: QueryFn, project_name: str) -> list[EndpointRow]:
    """The endpoints a project exposes, each with its handler and how many
    call sites in the whole graph reach it. Zero callers on a graph that
    holds only this project means "none indexed", not "dead"."""
    params = {cs.KEY_PROJECT_PREFIX: _prefix(project_name)}
    return [_endpoint_row(row) for row in fetch_all(cq.CYPHER_GRAPH_ENDPOINTS, params)]


def endpoint_callers(
    fetch_all: QueryFn, project_name: str, target: str
) -> list[EndpointCallerRow]:
    """Call sites, in any project, that reach the endpoint `target` names:
    the handler's qualified name or the endpoint identity (`GET /users/{id}`).

    Through a NETWORK resource that RESOLVES_TO the endpoint, or directly for
    the RPC and dispatch kinds, which join without RESOLVES_TO.
    """
    params = {cs.KEY_PROJECT_PREFIX: _prefix(project_name), cs.KEY_QN: target}
    rows = [
        _endpoint_caller_row(row)
        for query in (
            cq.CYPHER_GRAPH_ENDPOINT_CALLERS,
            cq.CYPHER_GRAPH_ENDPOINT_DIRECT_CALLERS,
        )
        for row in fetch_all(query, params)
    ]
    # Every field in the key: a caller that both reads and writes one URL is
    # two rows, and two handlers can share a caller (bot review on PR #1975).
    return sorted(
        rows,
        key=lambda r: (
            r["qualified_name"],
            r["url"] or "",
            r["path"] or "",
            r["direction"] or "",
            r["endpoint"],
            r["handler"],
        ),
    )


def remote_dependencies(
    fetch_all: QueryFn, project_name: str
) -> list[RemoteDependencyRow]:
    """Every network access a project makes, with the handler and project it
    resolves to; an unresolved row (no endpoint) is a dependency the graph
    cannot place -- a dynamic URL, or a service not indexed."""
    params = {cs.KEY_PROJECT_PREFIX: _prefix(project_name)}
    return [
        RemoteDependencyRow(
            label=str(row.get(cs.KEY_LABEL) or ""),
            qualified_name=str(row.get(cs.KEY_QUALIFIED_NAME) or ""),
            path=_opt_str(row.get(cs.KEY_PATH)),
            url=_opt_str(row.get(cs.KEY_URL)),
            direction=_opt_str(row.get(cs.KEY_DIRECTION)),
            endpoint=_opt_str(row.get(cs.KEY_ENDPOINT)),
            handler=_opt_str(row.get(cs.KEY_HANDLER)),
            handler_project=_opt_str(row.get(cs.KEY_HANDLER_PROJECT)),
        )
        for row in fetch_all(cq.CYPHER_GRAPH_REMOTE_DEPENDENCIES, params)
    ]
