"""Structural delta after a write (issue #1525).

An edit lands in the graph through the scoped re-ingest (issue #1524); this
module reads the touched files' subgraph before and after that re-ingest and
reports what the edit did to the structure: symbols added, removed and
renamed, callers left pointing at nothing, signature changes with an arity
verdict per call site, new duplicates of existing functions, new import
cycles, and the tests that reach the changed symbols. It is the in-memory
twin of `services/graph_diff.py`, which diffs exported indexes offline.

Everything is fixed Cypher scoped to one project plus client-side set
arithmetic over the fetched rows, so the report is deterministic and cheap:
the graph reads are linear in the touched files' edges, plus one linear
project scan each for the duplicate and test-reach indexes.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple, TypedDict

from . import constants as cs
from . import cypher_queries as cq
from .crash_correlation import ArityError, diagnose_arity
from .dead_code import (
    _is_test_symbol,
    _node_props,
    _NodeId,
    _rust_test_fn_spans,
    _rust_test_modules_from_nodes,
)
from .duplicates import _jaccard
from .graph_query import QueryFn, _prefix
from .types_defs import PropertyDict, ReingestReport, ResultRow

_METHOD_LABELS = frozenset({cs.NodeLabel.METHOD.value})


class Definition(NamedTuple):
    label: str
    qualified_name: str
    name: str
    path: str
    start_line: int
    end_line: int
    positional_params: tuple[str, ...] | None
    fingerprint: str
    fingerprint_nodes: int
    branches: frozenset[str]


class CallSite(NamedTuple):
    caller: str
    caller_path: str
    rel: str
    callee: str
    callee_path: str
    line: int | None
    col: int | None
    arg_count: int | None
    kwarg_names: tuple[str, ...]


class Snapshot(NamedTuple):
    """The touched files' subgraph plus the project's module import graph.

    `definitions` are the symbols defined in the touched files; `callees`
    the definitions elsewhere that the touched files' sites resolve to.
    """

    paths: frozenset[str]
    definitions: dict[str, Definition]
    callees: dict[str, Definition]
    sites: tuple[CallSite, ...]
    imports: dict[str, frozenset[str]]
    module_paths: dict[str, str]


class RenameFinding(TypedDict):
    old: str
    new: str
    path: str


class DanglingCaller(TypedDict):
    caller: str
    path: str
    line: int | None
    col: int | None
    target: str
    renamed_to: str | None


class ArityAtSite(TypedDict):
    caller: str
    path: str
    line: int | None
    col: int | None
    arg_count: int | None
    kwarg_names: list[str]
    declared_count: int
    verdict: str


class SignatureChange(TypedDict):
    qualified_name: str
    path: str
    before: list[str] | None
    after: list[str] | None
    sites: list[ArityAtSite]


class DuplicateOriginal(TypedDict):
    qualified_name: str
    path: str
    start_line: int


class NewDuplicate(TypedDict):
    qualified_name: str
    path: str
    start_line: int
    kind: str
    similarity: float
    original: DuplicateOriginal


class TestReach(TypedDict):
    qualified_name: str
    path: str | None
    depth: int
    through: str


class SymbolDelta(TypedDict):
    added: list[str]
    removed: list[str]
    renamed: list[RenameFinding]
    changed: list[str]


class StructuralDelta(TypedDict):
    paths: list[str]
    reparsed: list[str]
    affected: list[str]
    removed_files: list[str]
    symbols: SymbolDelta
    dangling_callers: list[DanglingCaller]
    signature_changes: list[SignatureChange]
    arity_findings: list[ArityAtSite]
    new_duplicates: list[NewDuplicate]
    new_import_cycles: list[list[str]]
    tests_reaching: list[TestReach]
    reingest_ms: float
    delta_ms: float


# --- snapshot -----------------------------------------------------------------


def _text(value: object) -> str:
    return str(value) if isinstance(value, str) else ""


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def normalise_paths(paths: Iterable[Path | str], repo_root: Path | None) -> list[str]:
    """Repo-relative POSIX paths, the form the graph stores in `path`."""
    out: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if repo_root is not None and path.is_absolute():
            try:
                path = path.resolve().relative_to(repo_root.resolve())
            except ValueError:
                continue
        out.add(path.as_posix())
    return sorted(out)


def _definition(row: ResultRow) -> Definition:
    params = row.get(cs.KEY_POSITIONAL_PARAMS)
    return Definition(
        label=_text(row.get(cs.KEY_LABEL)),
        qualified_name=_text(row.get(cs.KEY_QUALIFIED_NAME)),
        name=_text(row.get(cs.KEY_NAME)),
        path=_text(row.get(cs.KEY_PATH)),
        start_line=_int(row.get(cs.KEY_START_LINE)),
        end_line=_int(row.get(cs.KEY_END_LINE)),
        positional_params=_strings(params) if isinstance(params, list) else None,
        fingerprint=_text(row.get(cs.KEY_AST_FINGERPRINT)),
        fingerprint_nodes=_int(row.get(cs.KEY_AST_FINGERPRINT_NODES)),
        branches=frozenset(_strings(row.get(cs.KEY_AST_BRANCH_FINGERPRINTS))),
    )


def _site(row: ResultRow) -> CallSite:
    return CallSite(
        caller=_text(row.get(cs.KEY_FROM_QN)),
        caller_path=_text(row.get(cs.KEY_FROM_PATH)),
        rel=_text(row.get(cs.KEY_REL_TYPE)),
        callee=_text(row.get(cs.KEY_TO_QN)),
        callee_path=_text(row.get(cs.KEY_TO_PATH)),
        line=_opt_int(row.get(cs.KEY_LINE)),
        col=_opt_int(row.get(cs.KEY_COL)),
        arg_count=_opt_int(row.get(cs.KEY_ARG_COUNT)),
        kwarg_names=_strings(row.get(cs.KEY_KWARG_NAMES)),
    )


def snapshot(fetch_all: QueryFn, project_name: str, paths: Iterable[str]) -> Snapshot:
    """Read the subgraph of `paths` (repo-relative) and the module imports."""
    path_list = sorted(set(paths))
    params: PropertyDict = {
        cs.KEY_PROJECT_PREFIX: _prefix(project_name),
        cs.CYPHER_PARAM_PATHS: list(path_list),
    }
    definitions: dict[str, Definition] = {}
    for row in fetch_all(cq.CYPHER_DELTA_DEFINITIONS, params):
        definition = _definition(row)
        if definition.qualified_name:
            definitions[definition.qualified_name] = definition
    sites = tuple(
        site
        for site in (_site(row) for row in fetch_all(cq.CYPHER_DELTA_SITES, params))
        if site.caller and site.callee
    )
    missing = sorted({s.callee for s in sites} - set(definitions))
    callees: dict[str, Definition] = {}
    if missing:
        for row in fetch_all(cq.CYPHER_DELTA_DEFINITIONS_BY_QN, {cs.KEY_QNS: missing}):
            definition = _definition(row)
            if definition.qualified_name:
                callees[definition.qualified_name] = definition
    imports: dict[str, set[str]] = {}
    module_paths: dict[str, str] = {}
    for row in fetch_all(cq.CYPHER_DELTA_MODULE_IMPORTS, params):
        source = _text(row.get(cs.KEY_FROM_QN))
        target = _text(row.get(cs.KEY_TO_QN))
        if not source or not target:
            continue
        imports.setdefault(source, set()).add(target)
        imports.setdefault(target, set())
        module_paths[source] = _text(row.get(cs.KEY_FROM_PATH))
    return Snapshot(
        paths=frozenset(path_list),
        definitions=definitions,
        callees=callees,
        sites=sites,
        imports={qn: frozenset(targets) for qn, targets in imports.items()},
        module_paths=module_paths,
    )


# --- symbols ------------------------------------------------------------------


def _renames(
    removed: list[str], added: list[str], before: Snapshot, after: Snapshot
) -> list[RenameFinding]:
    # A rename keeps the body: the same whole-skeleton fingerprint under a
    # new name in the same file. Paired one-to-one in sorted order so a
    # duplicated body cannot be reported as two renames of one symbol.
    by_shape: dict[tuple[str, str], list[str]] = {}
    for qn in added:
        definition = after.definitions[qn]
        if definition.fingerprint:
            by_shape.setdefault((definition.path, definition.fingerprint), []).append(
                qn
            )
    renames: list[RenameFinding] = []
    for qn in removed:
        definition = before.definitions[qn]
        candidates = by_shape.get((definition.path, definition.fingerprint))
        if definition.fingerprint and candidates:
            renames.append(
                RenameFinding(old=qn, new=candidates.pop(0), path=definition.path)
            )
    return renames


def _changed(before: Snapshot, after: Snapshot) -> list[str]:
    changed: list[str] = []
    for qn in sorted(set(before.definitions) & set(after.definitions)):
        old, new = before.definitions[qn], after.definitions[qn]
        if (old.fingerprint, old.positional_params) != (
            new.fingerprint,
            new.positional_params,
        ):
            changed.append(qn)
    return changed


def _symbols(before: Snapshot, after: Snapshot) -> SymbolDelta:
    added = sorted(set(after.definitions) - set(before.definitions))
    removed = sorted(set(before.definitions) - set(after.definitions))
    renamed = _renames(removed, added, before, after)
    renamed_old = {r["old"] for r in renamed}
    renamed_new = {r["new"] for r in renamed}
    return SymbolDelta(
        added=[qn for qn in added if qn not in renamed_new],
        removed=[qn for qn in removed if qn not in renamed_old],
        renamed=renamed,
        changed=_changed(before, after),
    )


# --- dangling callers ---------------------------------------------------------


def _dangling(
    before: Snapshot, after: Snapshot, symbols: SymbolDelta
) -> list[DanglingCaller]:
    gone = set(symbols["removed"]) | {r["old"] for r in symbols["renamed"]}
    renamed_to = {r["old"]: r["new"] for r in symbols["renamed"]}
    after_pairs = {(site.caller, site.callee) for site in after.sites}
    out: list[DanglingCaller] = []
    seen: set[tuple[str, str, int | None, int | None]] = set()
    for site in before.sites:
        if site.callee not in gone:
            continue
        new_name = renamed_to.get(site.callee)
        # A caller re-parsed in this pass that now binds to the renamed
        # symbol was updated; every other caller still names what is gone.
        if site.caller_path in after.paths and (
            (new_name is not None and (site.caller, new_name) in after_pairs)
            or site.caller not in after.definitions
        ):
            continue
        key = (site.caller, site.callee, site.line, site.col)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            DanglingCaller(
                caller=site.caller,
                path=site.caller_path,
                line=site.line,
                col=site.col,
                target=site.callee,
                renamed_to=new_name,
            )
        )
    return sorted(
        out, key=lambda d: (d["path"], d["line"] or 0, d["col"] or 0, d["caller"])
    )


# --- signature changes --------------------------------------------------------


_VARIADIC = re.compile(r"(?<!\*)\*(?!\*)")


def _is_variadic(definition: Definition, repo_root: Path | None) -> bool:
    """Whether the Python definition's header declares `*args` or a bare `*`.

    `positional_params` ends at the star (CPython counts nothing after it),
    so the stored list alone cannot tell `f(a)` from `f(a, *rest)`; the
    header is read back so a variadic callee is never reported as
    receiving too many arguments.
    """
    if repo_root is None or not definition.path or definition.start_line < 1:
        return False
    try:
        lines = (
            (repo_root / definition.path)
            .read_text(encoding=cs.ENCODING_UTF8)
            .splitlines()
        )
    except (OSError, UnicodeDecodeError):
        return False
    header: list[str] = []
    for line in lines[definition.start_line - 1 : definition.end_line or None]:
        header.append(line)
        if ")" in line:
            break
    text = "\n".join(header)
    open_at = text.find("(")
    close_at = text.find(")", open_at + 1)
    if open_at < 0 or close_at < 0:
        return False
    return _VARIADIC.search(text[open_at:close_at]) is not None


def _arity_verdict(
    site: CallSite, definition: Definition, repo_root: Path | None
) -> tuple[int, str]:
    declared = definition.positional_params
    if declared is None:
        return -1, cs.DELTA_ARITY_UNKNOWN
    if site.arg_count is None:
        return len(declared), cs.DELTA_ARITY_UNKNOWN
    is_method = definition.label in _METHOD_LABELS
    passed = site.arg_count + len(site.kwarg_names) + (1 if is_method else 0)
    # `diagnose_arity` owns the receiver arithmetic (`self` counts for
    # CPython but is not caller-supplied); here the "message" is the site.
    verdict = diagnose_arity(
        ArityError(callee=definition.name, expected=passed, actual=passed),
        declared,
        is_method,
    )
    declared_count = verdict.declared_count - (1 if is_method else 0)
    if verdict.confirmed:
        return declared_count, cs.DELTA_ARITY_OK
    if passed > verdict.declared_count:
        if _is_variadic(definition, repo_root):
            return declared_count, cs.DELTA_ARITY_OK
        return declared_count, cs.DELTA_ARITY_TOO_MANY
    return declared_count, cs.DELTA_ARITY_POSSIBLY_MISSING


def _site_finding(
    site: CallSite, definition: Definition, repo_root: Path | None
) -> ArityAtSite:
    declared_count, verdict = _arity_verdict(site, definition, repo_root)
    return ArityAtSite(
        caller=site.caller,
        path=site.caller_path,
        line=site.line,
        col=site.col,
        arg_count=site.arg_count,
        kwarg_names=list(site.kwarg_names),
        declared_count=declared_count,
        verdict=verdict,
    )


def _site_order(finding: ArityAtSite) -> tuple[str, int, int]:
    return (finding["path"], finding["line"] or 0, finding["col"] or 0)


def _arity_findings(after: Snapshot, repo_root: Path | None) -> list[ArityAtSite]:
    """Call sites in the re-parsed files that pass more than the callee takes.

    The definitive verdict only: a site passing fewer positional arguments
    may be relying on defaults the graph does not record.
    """
    out: list[ArityAtSite] = []
    for site in after.sites:
        callee = after.definitions.get(site.callee) or after.callees.get(site.callee)
        if (
            callee is None
            or site.caller_path not in after.paths
            or site.rel != cs.RelationshipType.CALLS.value
        ):
            continue
        finding = _site_finding(site, callee, repo_root)
        if finding["verdict"] == cs.DELTA_ARITY_TOO_MANY:
            out.append(finding)
    return sorted(out, key=_site_order)


def _signature_changes(
    before: Snapshot, after: Snapshot, symbols: SymbolDelta, repo_root: Path | None
) -> list[SignatureChange]:
    out: list[SignatureChange] = []
    for qn in symbols["changed"]:
        old, new = before.definitions[qn], after.definitions[qn]
        if old.positional_params == new.positional_params:
            continue
        sites = [
            _site_finding(site, new, repo_root)
            for site in after.sites
            if site.callee == qn and site.rel == cs.RelationshipType.CALLS.value
        ]
        out.append(
            SignatureChange(
                qualified_name=qn,
                path=new.path,
                before=list(old.positional_params) if old.positional_params else None,
                after=list(new.positional_params) if new.positional_params else None,
                sites=sorted(sites, key=_site_order),
            )
        )
    return out


# --- new duplicates -----------------------------------------------------------


class _Shape(NamedTuple):
    qualified_name: str
    path: str
    start_line: int
    fingerprint: str
    nodes: int
    branches: frozenset[str]


def _shape(row: ResultRow) -> _Shape:
    return _Shape(
        qualified_name=_text(row.get(cs.KEY_QUALIFIED_NAME)),
        path=_text(row.get(cs.KEY_PATH)),
        start_line=_int(row.get(cs.KEY_START_LINE)),
        fingerprint=_text(row.get(cs.KEY_AST_FINGERPRINT)),
        nodes=_int(row.get(cs.KEY_AST_FINGERPRINT_NODES)),
        branches=frozenset(_strings(row.get(cs.KEY_AST_BRANCH_FINGERPRINTS))),
    )


def _match(
    candidate: _Shape, other: _Shape, threshold: float
) -> tuple[str, float] | None:
    if other.qualified_name == candidate.qualified_name:
        return None
    if other.fingerprint == candidate.fingerprint:
        return cs.KIND_EXACT, 1.0
    if not candidate.branches or not other.branches:
        return None
    similarity = _jaccard(candidate.branches, other.branches)
    if similarity >= threshold:
        return cs.KIND_SIMILAR, similarity
    return None


def _new_duplicates(
    fetch_all: QueryFn,
    project_name: str,
    fresh: Iterable[str],
    threshold: float = cs.DUPLICATES_DEFAULT_THRESHOLD,
    min_nodes: int = cs.DUPLICATES_DEFAULT_MIN_NODES,
) -> list[NewDuplicate]:
    fresh_set = set(fresh)
    if not fresh_set:
        return []
    rows = fetch_all(
        cq.CYPHER_DUPLICATE_FINGERPRINTS, {cs.KEY_PROJECT_PREFIX: _prefix(project_name)}
    )
    shapes = [
        s for s in (_shape(r) for r in rows) if s.fingerprint and s.nodes >= min_nodes
    ]
    out: list[NewDuplicate] = []
    for candidate in shapes:
        if candidate.qualified_name not in fresh_set:
            continue
        best: tuple[float, _Shape, str] | None = None
        for other in shapes:
            found = _match(candidate, other, threshold)
            # An existing symbol is the original; a fresh one is at best a
            # peer, reported once from the lexically earlier side.
            if found is None or (
                other.qualified_name in fresh_set
                and other.qualified_name < candidate.qualified_name
            ):
                continue
            kind, similarity = found
            if best is None or similarity > best[0]:
                best = (similarity, other, kind)
        if best is not None:
            similarity, other, kind = best
            out.append(
                NewDuplicate(
                    qualified_name=candidate.qualified_name,
                    path=candidate.path,
                    start_line=candidate.start_line,
                    kind=kind,
                    similarity=round(similarity, 3),
                    original=DuplicateOriginal(
                        qualified_name=other.qualified_name,
                        path=other.path,
                        start_line=other.start_line,
                    ),
                )
            )
    return sorted(out, key=lambda d: (d["path"], d["start_line"], d["qualified_name"]))


# --- import cycles ------------------------------------------------------------


def strongly_connected(graph: dict[str, frozenset[str]]) -> list[frozenset[str]]:
    """Tarjan's SCCs, iteratively (a module graph can be thousands deep)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[frozenset[str]] = []
    counter = 0
    for root in sorted(graph):
        if root in index:
            continue
        work: list[tuple[str, Iterable[str]]] = [
            (root, iter(sorted(graph.get(root, ()))))
        ]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component: set[str] = set()
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.add(member)
                    if member == node:
                        break
                out.append(frozenset(component))
    return out


def _cycles(graph: dict[str, frozenset[str]]) -> set[frozenset[str]]:
    return {
        component
        for component in strongly_connected(graph)
        if len(component) > 1
        or any(member in graph.get(member, ()) for member in component)
    }


def _new_import_cycles(before: Snapshot, after: Snapshot) -> list[list[str]]:
    touched = {qn for qn, path in after.module_paths.items() if path in after.paths}
    touched |= {qn for qn, path in before.module_paths.items() if path in before.paths}
    fresh = _cycles(after.imports) - _cycles(before.imports)
    return sorted(sorted(component) for component in fresh if component & touched)


# --- tests reaching -----------------------------------------------------------


class _Reach(NamedTuple):
    depth: dict[str, int]
    through: dict[str, str]
    nodes: dict[_NodeId, PropertyDict]


def _walk_callers(fetch_all: QueryFn, prefix: str, targets: set[str]) -> _Reach:
    """Multi-source backward BFS, one indexed query per hop.

    The cost is proportional to what is reached rather than to the project:
    a project-wide reverse call graph costs more to build than most deltas
    take in total.
    """
    depth = {qn: 0 for qn in targets}
    through = {qn: qn for qn in targets}
    nodes: dict[_NodeId, PropertyDict] = {}
    frontier = sorted(targets)
    for hop in range(1, cs.DELTA_REACH_MAX_DEPTH + 1):
        if not frontier:
            break
        rows = fetch_all(
            cq.CYPHER_DELTA_CALLERS_OF,
            {cs.KEY_PROJECT_PREFIX: prefix, cs.KEY_QNS: frontier},
        )
        next_frontier: list[str] = []
        for row in sorted(
            rows,
            key=lambda r: (
                _text(r.get(cs.KEY_QUALIFIED_NAME)),
                _text(r.get(cs.KEY_TO_QN)),
            ),
        ):
            qn = _text(row.get(cs.KEY_QUALIFIED_NAME))
            if not qn or qn in depth:
                continue
            depth[qn] = hop
            through[qn] = _text(row.get(cs.KEY_TO_QN))
            nodes[(_text(row.get(cs.KEY_LABEL)), qn)] = _node_props(row)
            next_frontier.append(qn)
        frontier = next_frontier
    return _Reach(depth, through, nodes)


def _rust_inputs(
    fetch_all: QueryFn, prefix: str, reached: dict[_NodeId, PropertyDict]
) -> tuple[set[str], dict[str, list[tuple[int, int]]]]:
    rust_paths = sorted(
        {
            str(props.get(cs.KEY_PATH))
            for props in reached.values()
            if str(props.get(cs.KEY_PATH, "")).endswith(cs.EXT_RS)
        }
    )
    if not rust_paths:
        return set(), {}
    modules: dict[_NodeId, PropertyDict] = {}
    for row in fetch_all(cq.CYPHER_DELTA_RUST_MODULES, {cs.KEY_PROJECT_PREFIX: prefix}):
        qn = _text(row.get(cs.KEY_QUALIFIED_NAME))
        if qn:
            modules[(_text(row.get(cs.KEY_LABEL)), qn)] = _node_props(row)
    functions: dict[_NodeId, PropertyDict] = {}
    for row in fetch_all(
        cq.CYPHER_DELTA_RUST_TEST_FNS,
        {cs.KEY_PROJECT_PREFIX: prefix, cs.CYPHER_PARAM_PATHS: rust_paths},
    ):
        qn = _text(row.get(cs.KEY_QUALIFIED_NAME))
        if qn:
            functions[(_text(row.get(cs.KEY_LABEL)), qn)] = _node_props(row)
    return _rust_test_modules_from_nodes(modules), _rust_test_fn_spans(functions)


def _tests_reaching(
    fetch_all: QueryFn, project_name: str, targets: Iterable[str]
) -> list[TestReach]:
    prefix = _prefix(project_name)
    reach = _walk_callers(fetch_all, prefix, set(targets))
    rust_modules, rust_spans = _rust_inputs(fetch_all, prefix, reach.nodes)
    out: list[TestReach] = []
    for (_label, raw_qn), props in reach.nodes.items():
        qn = str(raw_qn)
        path = str(props.get(cs.KEY_PATH) or "")
        if _is_test_symbol(
            props, qn, path, cs.TEST_PATH_PATTERNS, rust_modules, rust_spans
        ):
            out.append(
                TestReach(
                    qualified_name=qn,
                    path=path or None,
                    depth=reach.depth[qn],
                    through=reach.through[qn],
                )
            )
    return sorted(out, key=lambda r: (r["depth"], r["qualified_name"]))


# --- the delta ----------------------------------------------------------------


def structural_delta(
    fetch_all: QueryFn,
    project_name: str,
    before: Snapshot,
    after: Snapshot,
    report: ReingestReport | None = None,
    repo_root: Path | None = None,
) -> StructuralDelta:
    """Diff two snapshots of the same paths, then look up what they touch."""
    started = time.perf_counter()
    symbols = _symbols(before, after)
    fresh = set(symbols["added"]) | set(symbols["changed"])
    fresh |= {r["new"] for r in symbols["renamed"]}
    # A re-parsed file was edited: every symbol it defines may behave
    # differently even when its skeleton and signature did not move, so
    # the tests to run are those reaching any of them.
    touched = fresh | {
        qn for qn, d in after.definitions.items() if d.path in after.paths
    }
    return StructuralDelta(
        paths=sorted(before.paths | after.paths),
        reparsed=list(report.reparsed) if report else [],
        affected=list(report.affected) if report else [],
        removed_files=list(report.removed) if report else [],
        symbols=symbols,
        dangling_callers=_dangling(before, after, symbols),
        signature_changes=_signature_changes(before, after, symbols, repo_root),
        arity_findings=_arity_findings(after, repo_root),
        new_duplicates=_new_duplicates(fetch_all, project_name, fresh),
        new_import_cycles=_new_import_cycles(before, after),
        tests_reaching=_tests_reaching(fetch_all, project_name, touched)
        if touched
        else [],
        reingest_ms=round(report.elapsed_ms, 1) if report else 0.0,
        delta_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def observe(
    fetch_all: QueryFn,
    project_name: str,
    paths: Iterable[str],
    apply: Callable[[], ReingestReport],
    repo_root: Path | None = None,
) -> StructuralDelta:
    """Snapshot `paths`, run `apply` (the scoped re-ingest), snapshot, diff.

    The caller holds whatever lock serialises graph writes; both reads and
    the re-ingest must see one generation of the graph.
    """
    path_list = sorted(set(paths))
    started = time.perf_counter()
    before = snapshot(fetch_all, project_name, path_list)
    apply_started = time.perf_counter()
    report = apply()
    reingest_ms = (time.perf_counter() - apply_started) * 1000
    after = snapshot(fetch_all, project_name, path_list)
    delta = structural_delta(fetch_all, project_name, before, after, report, repo_root)
    # The re-ingest's own clock covers only its inner work; the caller sees
    # the wall time of the whole apply step, and `delta_ms` is everything
    # this function added on top of it: both snapshots and the diff.
    total_ms = (time.perf_counter() - started) * 1000
    delta["reingest_ms"] = round(max(delta["reingest_ms"], reingest_ms), 1)
    delta["delta_ms"] = round(total_ms - reingest_ms, 1)
    return delta


def has_findings(delta: StructuralDelta) -> bool:
    """True when the delta reports something an author should look at."""
    return bool(
        delta["dangling_callers"]
        or any(
            site["verdict"] != cs.DELTA_ARITY_OK
            and site["verdict"] != cs.DELTA_ARITY_UNKNOWN
            for change in delta["signature_changes"]
            for site in change["sites"]
        )
        or delta["arity_findings"]
        or delta["new_duplicates"]
        or delta["new_import_cycles"]
    )
