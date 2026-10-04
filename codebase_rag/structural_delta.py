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
project scan for the duplicate index; tests reaching a changed symbol are
found by walking its callers one hop at a time.
"""

from __future__ import annotations

import ast
import re
import time
from collections.abc import Callable, Iterable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple, TypedDict

from tree_sitter import Node, Tree

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
from .language_spec import get_language_for_extension
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
    # The shape fingerprint drops the keyword, so a `def` <-> `async def`
    # flip is told apart by this alone (issue #2860).
    is_async: bool = False


class CallSite(NamedTuple):
    caller: str
    caller_path: str
    rel: str
    # How the edge was bound and whether an argument spreads a sequence:
    # outside Python only an exact binding with a countable argument list
    # gets a definite arity verdict (issue #2517).
    resolution: str
    spread_args: bool
    # What a Rust or C# call is written through (`S` in `S::m(s)`, "" for a
    # value): decides whether a receiver sits in the argument list.
    call_qualifier: str | None
    callee: str
    callee_path: str
    line: int | None
    col: int | None
    arg_count: int | None
    kwarg_names: tuple[str, ...]
    star_args: bool
    # `**opts` at the site: it may supply any keyword, required ones too.
    star_kwargs: bool = False
    # How the edge was bound; absent on a legacy edge, which ranks as exact.
    resolution: str = ""


class ImportBinding(NamedTuple):
    """One name an import statement binds: `from module import name as bound`."""

    importer: str
    importer_path: str
    module: str
    imported_name: str
    bound: str
    line: int | None
    col: int | None
    # The file of the module named; empty for one outside the project.
    module_path: str = ""

    @property
    def symbol(self) -> str:
        return f"{self.module}{cs.SEPARATOR_DOT}{self.imported_name}"


class Snapshot(NamedTuple):
    """The touched files' subgraph plus the project's module import graph.

    `definitions` are the symbols defined in the touched files; `callees`
    the definitions elsewhere that the touched files' sites resolve to;
    `bindings` the by-name imports into and out of the touched files.
    """

    paths: frozenset[str]
    definitions: dict[str, Definition]
    callees: dict[str, Definition]
    sites: tuple[CallSite, ...]
    imports: dict[str, frozenset[str]]
    module_paths: dict[str, str]
    bindings: tuple[ImportBinding, ...] = ()


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


class DanglingImporter(TypedDict):
    """An import statement or `__all__` entry still naming a gone symbol."""

    importer: str
    path: str
    line: int | None
    col: int | None
    kind: cs.DanglingImportKind
    name: str
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
    # The definition the site was judged against and how its edge was bound
    # (issue #2639): a finding against a guess can be told from a real one.
    callee: str
    resolution: str


class RemoteCaller(TypedDict):
    """A call site in another service reaching a changed handler's endpoint."""

    qualified_name: str
    label: str
    path: str
    url: str
    endpoint: str


class SignatureChange(TypedDict):
    qualified_name: str
    path: str
    before: list[str] | None
    after: list[str] | None
    sites: list[ArityAtSite]
    # `added` or `removed` when the definition turned `async` or back
    # (issue #2860), else None.
    async_change: str | None
    # Call sites across the network that reach this definition's endpoint
    # (issue #1603): a signature change on a handler is a contract change
    # for them, and no CALLS edge would ever list them.
    remote_callers: list[RemoteCaller]


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


class SiteCounts(TypedDict):
    """CALLS sites into the touched files' definitions, before and after."""

    before: int
    after: int


class SymbolDelta(TypedDict):
    added: list[str]
    removed: list[str]
    renamed: list[RenameFinding]
    changed: list[str]


class StaleImporter(TypedDict):
    """A module still importing from where a moved symbol used to live."""

    importer: str
    path: str
    line: int


class StructuralDelta(TypedDict):
    paths: list[str]
    reparsed: list[str]
    affected: list[str]
    removed_files: list[str]
    symbols: SymbolDelta
    dangling_callers: list[DanglingCaller]
    dangling_importers: list[DanglingImporter]
    signature_changes: list[SignatureChange]
    arity_findings: list[ArityAtSite]
    new_duplicates: list[NewDuplicate]
    new_import_cycles: list[list[str]]
    # Importers that still name a module a moved symbol has left. Empty for
    # every operation that moves nothing, so a non-move never carries one.
    stale_importers: list[StaleImporter]
    tests_reaching: list[TestReach]
    call_sites: SiteCounts
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
        is_async=cs.TS_PY_ASYNC in _strings(row.get(cs.KEY_MODIFIERS)),
    )


def _site(row: ResultRow) -> CallSite:
    qualifier = row.get(cs.KEY_CALL_QUALIFIER)
    return CallSite(
        caller=_text(row.get(cs.KEY_FROM_QN)),
        caller_path=_text(row.get(cs.KEY_FROM_PATH)),
        rel=_text(row.get(cs.KEY_REL_TYPE)),
        resolution=_text(row.get(cs.KEY_RESOLUTION)),
        spread_args=row.get(cs.KEY_SPREAD_ARGS) is True,
        call_qualifier=qualifier if isinstance(qualifier, str) else None,
        callee=_text(row.get(cs.KEY_TO_QN)),
        callee_path=_text(row.get(cs.KEY_TO_PATH)),
        line=_opt_int(row.get(cs.KEY_LINE)),
        col=_opt_int(row.get(cs.KEY_COL)),
        arg_count=_opt_int(row.get(cs.KEY_ARG_COUNT)),
        kwarg_names=_strings(row.get(cs.KEY_KWARG_NAMES)),
        star_args=row.get(cs.KEY_STAR_ARGS) is True,
        star_kwargs=row.get(cs.KEY_STAR_KWARGS) is True,
        resolution=_text(row.get(cs.KEY_RESOLUTION)),
    )


def _binding(row: ResultRow) -> ImportBinding:
    imported_name = _text(row.get(cs.KEY_IMPORTED_NAME))
    return ImportBinding(
        importer=_text(row.get(cs.KEY_FROM_QN)),
        importer_path=_text(row.get(cs.KEY_FROM_PATH)),
        module=_text(row.get(cs.KEY_TO_QN)),
        imported_name=imported_name,
        # A wildcard binds no single name and records no alias.
        bound=_text(row.get(cs.KEY_ALIAS)) or imported_name,
        line=_opt_int(row.get(cs.KEY_LINE)),
        col=_opt_int(row.get(cs.KEY_COL)),
        module_path=_text(row.get(cs.KEY_TO_PATH)),
    )


def _named_import_bindings(
    fetch_all: QueryFn, params: PropertyDict
) -> tuple[ImportBinding, ...]:
    """The named imports of the snapshot's modules (issue #2516)."""
    return tuple(
        binding
        for binding in (
            _binding(row) for row in fetch_all(cq.CYPHER_DELTA_NAMED_IMPORTS, params)
        )
        if binding.importer and binding.module and binding.imported_name
    )


def _longer_project_prefixes(fetch_all: QueryFn, project_name: str) -> tuple[str, ...]:
    """Return registered project names that can own a longer qualified name."""
    requested_prefix = f"{project_name}{cs.SEPARATOR_DOT}"
    names = {
        name
        for row in fetch_all(cq.CYPHER_LIST_PROJECTS, None)
        if isinstance(name := row.get(cs.KEY_NAME), str)
        and name.startswith(requested_prefix)
    }
    return tuple(sorted(names))


def _has_longer_project_owner(
    qualified_name: str, longer_project_prefixes: Iterable[str]
) -> bool:
    return any(
        qualified_name == project_name
        or qualified_name.startswith(f"{project_name}{cs.SEPARATOR_DOT}")
        for project_name in longer_project_prefixes
    )


def snapshot(
    fetch_all: QueryFn,
    project_name: str,
    paths: Iterable[str],
    *,
    longer_project_prefixes: tuple[str, ...] | None = None,
) -> Snapshot:
    """Read the subgraph of `paths` (repo-relative) and the module imports."""
    path_list = sorted(set(paths))
    longer_prefixes = (
        _longer_project_prefixes(fetch_all, project_name)
        if longer_project_prefixes is None
        else longer_project_prefixes
    )
    params: PropertyDict = {
        cs.KEY_PROJECT_PREFIX: _prefix(project_name),
        cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_prefixes),
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
        for row in fetch_all(
            cq.CYPHER_DELTA_DEFINITIONS_BY_QN,
            {
                cs.KEY_QNS: missing,
                cs.KEY_PROJECT_PREFIX: _prefix(project_name),
                cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_prefixes),
            },
        ):
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
        bindings=_named_import_bindings(fetch_all, params),
    )


# --- symbols ------------------------------------------------------------------


def _pair_by_shape(
    removed: list[str], by_shape: dict[tuple[str, str], list[str]], before: Snapshot
) -> tuple[list[RenameFinding], list[str]]:
    """Pass 1: same fingerprint, same file, new name -- a plain rename."""
    renames: list[RenameFinding] = []
    unpaired: list[str] = []
    for qn in removed:
        definition = before.definitions[qn]
        candidates = by_shape.get((definition.path, definition.fingerprint))
        if definition.fingerprint and candidates:
            renames.append(
                RenameFinding(old=qn, new=candidates.pop(0), path=definition.path)
            )
        else:
            unpaired.append(qn)
    return renames, unpaired


def _pair_by_move(
    unpaired: list[str],
    by_shape: dict[tuple[str, str], list[str]],
    before: Snapshot,
    after: Snapshot,
) -> tuple[list[RenameFinding], list[str]]:
    """Pass 2: same name and body, different file -- a move (issue #1534)."""
    by_name_shape: dict[tuple[str, str], list[str]] = {}
    for candidates in by_shape.values():
        for qn in candidates:
            definition = after.definitions[qn]
            by_name_shape.setdefault(
                (definition.name, definition.fingerprint), []
            ).append(qn)
    renames: list[RenameFinding] = []
    still_unpaired: list[str] = []
    for qn in unpaired:
        definition = before.definitions[qn]
        candidates = by_name_shape.get((definition.name, definition.fingerprint))
        if definition.fingerprint and candidates:
            new = candidates.pop(0)
            by_shape[(after.definitions[new].path, definition.fingerprint)].remove(new)
            renames.append(RenameFinding(old=qn, new=new, path=definition.path))
        else:
            still_unpaired.append(qn)
    return renames, still_unpaired


def _carried_container(
    qn: str,
    renames: list[RenameFinding],
    paired_new: set[str],
    added: list[str],
    before: Snapshot,
    after: Snapshot,
) -> str | None:
    """The added container a removed one moved to, when its members agree.

    A class carries no fingerprint of its own, so a renamed class reads as
    removed plus added; its methods do carry one and were paired already.
    """
    definition = before.definitions[qn]
    if definition.fingerprint:
        return None
    prefix = qn + cs.SEPARATOR_DOT
    targets = {
        r["new"][: len(r["new"]) - (len(r["old"]) - len(qn))]
        for r in renames
        if r["old"].startswith(prefix)
    }
    if len(targets) != 1:
        return None
    (target,) = targets
    candidate = after.definitions.get(target)
    if (
        candidate is None
        or candidate.fingerprint
        or candidate.label != definition.label
        or target in paired_new
        or target not in added
    ):
        return None
    return target


def _pair_lone_containers(
    still_unpaired: list[str],
    renames: list[RenameFinding],
    paired_new: set[str],
    added: list[str],
    before: Snapshot,
    after: Snapshot,
    declared: frozenset[tuple[str, str]],
) -> list[RenameFinding]:
    """Pass 4: an EMPTY container, paired only when the caller DECLARED it.

    An empty container has no fingerprint and no descendants, and the only
    thing that changed is its name -- so nothing in the two snapshots can
    tell a rename from a replacement. They are the same edit. Neither
    content similarity (git's heuristic) nor tree-sitter's changed ranges
    separates them, because the difference is intent, not syntax.

    So this pass does not infer. An operation that RENAMED something knows
    which pairs it applied and passes them in `declared`; anything else is
    reported as a removal plus an addition, which is the truth about what
    the snapshots show. Guessing here produced renames that never happened,
    and the contract treats an unexpected rename as a failure, so an
    invented one rolls back a correct edit (Greptile, PR #1547).
    """
    lone_removed = [
        qn
        for qn in still_unpaired
        if not before.definitions[qn].fingerprint
        and not any(r["old"] == qn for r in renames)
    ]
    lone_added = [
        qn
        for qn in added
        if qn not in paired_new and not after.definitions[qn].fingerprint
    ]
    found: list[RenameFinding] = []
    for qn in lone_removed:
        definition = before.definitions[qn]
        matches = [
            other
            for other in lone_added
            if (qn, other) in declared
            and after.definitions[other].path == definition.path
            and after.definitions[other].label == definition.label
        ]
        if len(matches) != 1:
            continue
        target = matches[0]
        peers = [other for other in lone_removed if (other, target) in declared]
        if len(peers) == 1:
            found.append(RenameFinding(old=qn, new=target, path=definition.path))
            paired_new.add(target)
            lone_added.remove(target)
    return found


def _renames(
    removed: list[str],
    added: list[str],
    before: Snapshot,
    after: Snapshot,
    declared: frozenset[tuple[str, str]] = frozenset(),
) -> list[RenameFinding]:
    # A rename keeps the body: the same whole-skeleton fingerprint under a
    # new name in the same file. Paired one-to-one in sorted order so a
    # duplicated body cannot be reported as two renames of one symbol.
    #
    # Four passes, each narrower than the last and each extracted to its own
    # function (S3776): plain rename, move, container carried by its members,
    # then the lone empty container.
    by_shape: dict[tuple[str, str], list[str]] = {}
    for qn in added:
        definition = after.definitions[qn]
        if definition.fingerprint:
            by_shape.setdefault((definition.path, definition.fingerprint), []).append(
                qn
            )

    renames, unpaired = _pair_by_shape(removed, by_shape, before)
    moved, still_unpaired = _pair_by_move(unpaired, by_shape, before, after)
    renames.extend(moved)

    paired_new = {r["new"] for r in renames}
    for qn in still_unpaired:
        target = _carried_container(qn, renames, paired_new, added, before, after)
        if target is not None:
            renames.append(
                RenameFinding(old=qn, new=target, path=before.definitions[qn].path)
            )
            paired_new.add(target)

    renames.extend(
        _pair_lone_containers(
            still_unpaired, renames, paired_new, added, before, after, declared
        )
    )
    return renames


def _language(path: str) -> cs.SupportedLanguage | None:
    return get_language_for_extension(Path(path).suffix) if path else None


def _params_moved(old: Definition, new: Definition) -> bool:
    """Whether the declared parameters differ between the two snapshots.

    Outside Python a list on one side only was never extracted on the other
    (a graph indexed before issue #2517), which is no signature change.
    """
    if old.positional_params == new.positional_params:
        return False
    if _language(new.path) not in cs.DECLARED_ARITY_LANGUAGES:
        return True
    return old.positional_params is not None and new.positional_params is not None


def _changed(before: Snapshot, after: Snapshot) -> list[str]:
    changed: list[str] = []
    for qn in sorted(set(before.definitions) & set(after.definitions)):
        old, new = before.definitions[qn], after.definitions[qn]
        if (
            old.fingerprint != new.fingerprint
            or _params_moved(old, new)
            or old.is_async != new.is_async
        ):
            changed.append(qn)
    return changed


def _symbols(
    before: Snapshot,
    after: Snapshot,
    declared: frozenset[tuple[str, str]] = frozenset(),
) -> SymbolDelta:
    added = sorted(set(after.definitions) - set(before.definitions))
    removed = sorted(set(before.definitions) - set(after.definitions))
    renamed = _renames(removed, added, before, after, declared)
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


# --- dangling importers ------------------------------------------------------


# What a module binds that the graph cannot show, read from its source
# (Python only): its `def`s, classes and assignments, a `try`/`if` branch
# included, and the names it takes by `from x import y`, which are only as
# good as `x`. `*` among the latter marks a wildcard import.
class _SourceNames(NamedTuple):
    own: frozenset[str]
    imported: frozenset[str]
    # What `from module import *` takes: the strings of its `__all__`, or
    # None for a module without one (every name not starting with `_`) or
    # with one built at run time, which `star_unknown` tells apart.
    star: frozenset[str] | None = None
    star_unknown: bool = False


# The statement lists of a compound statement, walked as the module's own
# top level; every other field is an expression that may bind a name.
_PY_BLOCK_FIELDS = frozenset({"body", "orelse", "finalbody", "handlers", "cases"})


def _stored_names(node: ast.AST) -> Iterator[str]:
    """The names an expression or target writes in the scope it runs in.

    A lambda's body and a comprehension's own variables are scopes of their
    own in Python 3, so `[helper for helper in items]` binds no module-level
    `helper` (Greptile, PR #2574); `:=` inside a comprehension still binds
    in the enclosing scope (PEP 572), and so does a `case` capture or a
    lambda's default values, which run where the lambda is made.
    """
    pending = [node]
    while pending:
        current = pending.pop()
        if isinstance(current, ast.Name) and isinstance(current.ctx, ast.Store):
            yield current.id
        elif captured := _pattern_capture(current):
            yield captured
        pending.extend(_same_scope_children(current))


def _pattern_capture(node: ast.AST) -> str | None:
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name
    return node.rest if isinstance(node, ast.MatchMapping) else None


def _same_scope_children(node: ast.AST) -> Iterator[ast.AST]:
    if isinstance(node, ast.Lambda):
        # `lambda x=(helper := keep): x` binds `helper` where it is made
        # (Greptile, PR #2574); a missing keyword-only default is None.
        defaults = (*node.args.defaults, *node.args.kw_defaults)
        yield from (default for default in defaults if default is not None)
        return
    for field, value in ast.iter_fields(node):
        if isinstance(node, ast.comprehension) and field == "target":
            continue
        for part in value if isinstance(value, list) else [value]:
            if isinstance(part, ast.AST):
                yield part


def _literal_strings(node: ast.AST | None) -> frozenset[str] | None:
    """The strings of a list or tuple display of string literals, else None."""
    if not isinstance(node, ast.List | ast.Tuple):
        return None
    strings = [
        e.value
        for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    ]
    return frozenset(strings) if len(strings) == len(node.elts) else None


def _dunder_all_write(node: ast.AST) -> tuple[bool, frozenset[str] | None]:
    """Whether a statement writes `__all__`, and the names if they are literal.

    Assignment, `+=`, `.extend([...])` and `.append("...")` are read; anything
    else that writes it is a run-time value.
    """
    match node:
        case ast.Assign(targets=targets, value=value) if any(
            isinstance(t, ast.Name) and t.id == cs.PY_DUNDER_ALL for t in targets
        ):
            return True, _literal_strings(value)
        case (
            ast.AnnAssign(target=ast.Name(id=cs.PY_DUNDER_ALL), value=value)
            | ast.AugAssign(target=ast.Name(id=cs.PY_DUNDER_ALL), value=value)
        ):
            return True, _literal_strings(value)
        case ast.Expr(
            value=ast.Call(
                func=ast.Attribute(value=ast.Name(id=cs.PY_DUNDER_ALL), attr=method),
                args=[argument],
            )
        ) if method in (cs.PY_LIST_EXTEND, cs.PY_LIST_APPEND):
            if method == cs.PY_LIST_EXTEND:
                return True, _literal_strings(argument)
            return True, _literal_strings(ast.List(elts=[argument]))
    return False, None


def _names_bound_by(node: ast.AST, pending: list[ast.AST]) -> Iterator[str]:
    """The names one top-level statement binds, other than by `from` import.

    The statement lists nested in it (an `if` body, a `try` handler) are
    queued on `pending`: they run at import time too.
    """
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield from _definition_bindings(node)
        return
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.asname or alias.name.partition(cs.SEPARATOR_DOT)[0]
        return
    for field, value in ast.iter_fields(node):
        if field in _PY_BLOCK_FIELDS:
            pending.extend(value)
            continue
        for part in value if isinstance(value, list) else [value]:
            if isinstance(part, ast.AST):
                yield from _stored_names(part)


def _definition_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> Iterator[str]:
    """The names a `def` or `class` statement binds where it runs.

    Its own name, and any `:=` in what it evaluates there: its decorators,
    and a function's default values or a class's bases and keywords, so
    `def f(x=(helper := keep))` binds `helper` in the module, as a lambda's
    defaults do. The body and the parameters are scopes of their own.
    Annotations are left out: under `from __future__ import annotations`,
    and from Python 3.14 on, they are not evaluated there.
    """
    yield node.name
    if isinstance(node, ast.ClassDef):
        keywords = [keyword.value for keyword in node.keywords]
        evaluated = [*node.decorator_list, *node.bases, *keywords]
    else:
        kw_defaults = [d for d in node.args.kw_defaults if d is not None]
        evaluated = [*node.decorator_list, *node.args.defaults, *kw_defaults]
    for part in evaluated:
        yield from _stored_names(part)


def _python_top_level_names(text: str) -> _SourceNames | None:
    """The names a Python module binds at import time, or None if unparsable."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    own: set[str] = set()
    imported: set[str] = set()
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.asname or alias.name for alias in node.names)
        else:
            own.update(_names_bound_by(node, pending))
    star, star_unknown = _star_names(tree.body)
    return _SourceNames(
        own=frozenset(own),
        imported=frozenset(imported),
        star=star,
        star_unknown=star_unknown,
    )


def _in_source_order(
    statements: Iterable[ast.AST], in_branch: bool = False
) -> Iterator[tuple[ast.AST, bool]]:
    """Each statement that runs at import time, in source order.

    The flag says whether it sits in a statement list nested in another (an
    `if` or loop body, a `try` handler), which may not run; a `def` or
    `class` body is not import-time module code and is not entered.
    """
    for node in statements:
        yield node, in_branch
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for field, value in ast.iter_fields(node):
            if field in _PY_BLOCK_FIELDS:
                yield from _in_source_order(value, True)


def _star_names(
    statements: list[ast.stmt],
) -> tuple[frozenset[str] | None, bool]:
    """The `star` and `star_unknown` of `_SourceNames`, from `__all__` writes.

    Read in source order: an assignment at the top level replaces every
    write before it (Greptile, PR #2574), while one in a branch may not run,
    so the names it lists join the earlier ones -- a name any write that may
    survive lists may be in the final `__all__`, and only the rest are out.
    """
    star: set[str] | None = None
    unknown = False
    for node, in_branch in _in_source_order(statements):
        writes_all, listed = _dunder_all_write(node)
        if not writes_all:
            continue
        if not in_branch and _replaces_dunder_all(node):
            star, unknown = set(), False
        star = (star or set()) | (listed or set())
        unknown |= listed is None
    return (None if star is None else frozenset(star)), unknown


def _replaces_dunder_all(node: ast.AST) -> bool:
    """Whether a statement that writes `__all__` rebinds it to a new value."""
    return isinstance(node, ast.Assign) or (
        isinstance(node, ast.AnnAssign) and node.value is not None
    )


def _star_takes(names: _SourceNames | None, name: str) -> bool:
    """Whether `from module import *` binds `name`, by the module's source.

    Its `__all__` when it has one, else every name not starting with `_`
    (Greptile, PR #2574). A module that is not Python, cannot be read or
    builds `__all__` at run time sets no limit that can be checked.
    """
    if names is None or names.star_unknown:
        return True
    if names.star is None:
        return not name.startswith(cs.PY_PRIVATE_PREFIX)
    return name in names.star


# A module the edit left alone is read on demand: the definitions the graph
# records in its file, and that file's own by-name imports.
_ModuleLoader = Callable[[str], tuple[frozenset[str], tuple[ImportBinding, ...]]]


def _module_loader(fetch_all: QueryFn, params: PropertyDict) -> _ModuleLoader:
    def load(path: str) -> tuple[frozenset[str], tuple[ImportBinding, ...]]:
        scoped: PropertyDict = {**params, cs.CYPHER_PARAM_PATHS: [path]}
        definitions = frozenset(
            qn
            for row in fetch_all(cq.CYPHER_DELTA_DEFINITIONS, scoped)
            if (qn := _definition(row).qualified_name)
        )
        bindings = tuple(
            binding
            for binding in _named_import_bindings(fetch_all, scoped)
            if binding.importer_path == path
        )
        return definitions, bindings

    return load


class _AfterBindings:
    """Which names the modules of the project still bind after the edit.

    The after-snapshot holds the touched files only; a module the edit left
    alone is read through `load` the first time a re-export leads into it.
    """

    def __init__(
        self,
        after: Snapshot,
        gone: set[str],
        load: _ModuleLoader,
        repo_root: Path | None,
    ) -> None:
        self._gone = gone
        self._load = load
        self._repo_root = repo_root
        defined: dict[str, set[str]] = {path: set() for path in after.paths}
        for qn, definition in after.definitions.items():
            if definition.path in defined:
                defined[definition.path].add(qn)
        self._definitions = {path: frozenset(qns) for path, qns in defined.items()}
        self._bindings: dict[str, tuple[ImportBinding, ...]] = {
            path: tuple(b for b in after.bindings if b.importer_path == path)
            for path in after.paths
        }
        self._sources: dict[str, str | None] = {}
        self._names: dict[str, _SourceNames | None] = {}

    def source(self, path: str) -> str | None:
        if path not in self._sources:
            self._sources[path] = (
                _python_source(self._repo_root, path) if self._repo_root else None
            )
        return self._sources[path]

    def _source_names(self, path: str) -> _SourceNames | None:
        if path not in self._names:
            text = self.source(path)
            self._names[path] = None if text is None else _python_top_level_names(text)
        return self._names[path]

    def _facts(self, path: str) -> tuple[frozenset[str], tuple[ImportBinding, ...]]:
        if path not in self._definitions:
            self._definitions[path], self._bindings[path] = self._load(path)
        return self._definitions[path], self._bindings[path]

    def binds(
        self,
        module: str,
        path: str,
        name: str,
        seen: frozenset[tuple[str, str]] = frozenset(),
    ) -> bool:
        """Whether `module` (in file `path`) still binds `name` to something.

        Its own definition counts, and so does an import of the name that
        still resolves: after a move, `from app.util import helper` left in
        `app.core` keeps every `from app.core import helper` working, while
        `from app.empty import helper` keeps nothing when `app.empty` has no
        `helper`. A chain of re-exports is followed, into modules the edit
        left alone too; a cycle in it binds nothing.
        """
        if (module, name) in seen:
            return False
        seen = seen | {(module, name)}
        qn = f"{module}{cs.SEPARATOR_DOT}{name}"
        if not path:
            # Outside the project (`from os.path import join as helper`):
            # nothing to check the name against, so the import is taken
            # at its word unless it names a symbol the edit removed.
            return qn not in self._gone
        definitions, bindings = self._facts(path)
        names = self._source_names(path)
        if qn in definitions or (names is not None and _binds_itself(names, name)):
            return True
        own = [binding for binding in bindings if binding.importer == module]
        if any(self._resolves(binding, name, seen) for binding in own):
            return True
        return names is not None and _unfollowed_import(names, name, own)

    def _resolves(
        self, binding: ImportBinding, name: str, seen: frozenset[tuple[str, str]]
    ) -> bool:
        if binding.imported_name == cs.IMPORTED_NAME_WILDCARD:
            return _star_takes(
                self._source_names(binding.module_path), name
            ) and self.binds(binding.module, binding.module_path, name, seen)
        return binding.bound == name and (
            _imports_the_module_itself(binding, self._gone)
            or self.binds(
                binding.module, binding.module_path, binding.imported_name, seen
            )
        )


# A module-level `__getattr__` (PEP 562) can answer for any name.
_PY_MODULE_GETATTR = "__getattr__"


def _binds_itself(names: _SourceNames, name: str) -> bool:
    return name in names.own or _PY_MODULE_GETATTR in names.own


def _unfollowed_import(
    names: _SourceNames, name: str, bindings: list[ImportBinding]
) -> bool:
    """An import in the source that the graph holds no edge for.

    An unresolvable relative import, say: it cannot be followed, so it is
    taken at its word rather than reported.
    """
    wildcard = cs.IMPORTED_NAME_WILDCARD
    if name in names.imported and not any(
        b.bound == name and b.imported_name != wildcard for b in bindings
    ):
        return True
    return wildcard in names.imported and not any(
        b.imported_name == wildcard for b in bindings
    )


def _imports_the_module_itself(binding: ImportBinding, gone: set[str]) -> bool:
    """`from app import helpers`: the edge names the submodule `app.helpers`.

    Its `imported_name` is the module's own last segment, so the binding
    holds the module, not a name inside it -- unless that symbol existed
    and the edit removed it.
    """
    return (
        binding.module.rpartition(cs.SEPARATOR_DOT)[2] == binding.imported_name
        and binding.symbol not in gone
    )


def _opens_comment(line_prefix: str) -> bool:
    """Whether a `#` outside a string literal starts a comment in the text."""
    quote = ""
    for char in line_prefix:
        if quote:
            quote = "" if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "#":
            return True
    return False


def _dunder_all_entries(text: str, name: str) -> list[tuple[int, int]]:
    """(line, byte column) of each `__all__` string entry that reads `name`.

    A string in a comment (`# 'helper' was removed`) exports nothing.
    """
    found: list[tuple[int, int]] = []
    for block in re.finditer(cs.PY_DUNDER_ALL_BLOCK_PATTERN, text, re.S):
        for entry in re.finditer(cs.PY_DUNDER_ALL_ENTRY_PATTERN, block.group(1)):
            if entry.group("name") != name:
                continue
            offset = block.start(1) + entry.start("name")
            line_start = text.rfind("\n", 0, offset) + 1
            # The opening quote is not part of the prefix that is scanned.
            if _opens_comment(text[line_start : block.start(1) + entry.start()]):
                continue
            found.append(
                (
                    text.count("\n", 0, offset) + 1,
                    len(text[line_start:offset].encode(cs.ENCODING_UTF8)),
                )
            )
    return found


def _python_source(repo_root: Path, path: str) -> str | None:
    if get_language_for_extension(Path(path).suffix) != cs.SupportedLanguage.PYTHON:
        return None
    try:
        return (repo_root / path).read_text(encoding=cs.ENCODING_UTF8)
    except (OSError, UnicodeDecodeError):
        return None


def _import_findings(
    before: Snapshot,
    after: Snapshot,
    gone: set[str],
    renamed_to: dict[str, str],
    still: _AfterBindings,
) -> list[DanglingImporter]:
    # An importer the edit left alone still holds the statement the base
    # graph recorded, even when the module it names was deleted and the
    # edge went with it; an importer the edit touched is read after the
    # edit, at its new positions, so a statement it dropped is not listed.
    candidates = [
        *(b for b in before.bindings if b.importer_path not in after.paths),
        *(b for b in after.bindings if b.importer_path in after.paths),
    ]
    return [
        DanglingImporter(
            importer=binding.importer,
            path=binding.importer_path,
            line=binding.line,
            col=binding.col,
            kind=cs.DanglingImportKind.IMPORT,
            name=binding.imported_name,
            target=binding.symbol,
            renamed_to=renamed_to.get(binding.symbol),
        )
        for binding in candidates
        if binding.symbol in gone
        and not still.binds(binding.module, binding.module_path, binding.imported_name)
    ]


def _all_findings(
    before: Snapshot,
    after: Snapshot,
    gone: set[str],
    renamed_to: dict[str, str],
    still: _AfterBindings,
) -> list[DanglingImporter]:
    # (module, path, the name it exported the symbol under, the symbol): the
    # defining module of each module-level gone symbol, plus every module
    # that imported one by name, whether or not the edit kept that import.
    exporters: set[tuple[str, str, str, str]] = set()
    for qn in gone:
        definition = before.definitions.get(qn)
        module = _module_of(qn)
        # A method or nested function has its parent among the same file's
        # definitions, and only a module-level name can be in `__all__`.
        if definition is None or not module or module in before.definitions:
            continue
        exporters.add((module, definition.path, definition.name, qn))
    for binding in (*before.bindings, *after.bindings):
        if binding.symbol in gone:
            exporters.add(
                (binding.importer, binding.importer_path, binding.bound, binding.symbol)
            )
    out: list[DanglingImporter] = []
    for module, path, name, target in sorted(exporters):
        if still.binds(module, path, name):
            continue
        text = still.source(path)
        if text is None:
            continue
        out.extend(
            DanglingImporter(
                importer=module,
                path=path,
                line=line,
                col=col,
                kind=cs.DanglingImportKind.ALL,
                name=name,
                target=target,
                renamed_to=renamed_to.get(target),
            )
            for line, col in _dunder_all_entries(text, name)
        )
    return out


def _dangling_importers(
    before: Snapshot,
    after: Snapshot,
    symbols: SymbolDelta,
    repo_root: Path | None,
    load: _ModuleLoader,
) -> list[DanglingImporter]:
    """Import statements and `__all__` entries naming a removed symbol.

    `dangling_callers` needs a call or reference site, and a package
    `__init__` that only re-exports a name has none, so deleting the name
    left the package failing at import time with nothing reported (issue
    #2516). The IMPORTS edge records the name it binds; an entry is listed
    when that name, or an `__all__` string exporting it, points at a symbol
    the edit removed or renamed and nothing still binds it in its place.
    """
    gone = set(symbols["removed"]) | {r["old"] for r in symbols["renamed"]}
    if not gone:
        return []
    renamed_to = {r["old"]: r["new"] for r in symbols["renamed"]}
    still = _AfterBindings(after, gone, load, repo_root)
    found = _import_findings(before, after, gone, renamed_to, still)
    if repo_root is not None:
        found.extend(_all_findings(before, after, gone, renamed_to, still))
    unique = {
        (d["kind"], d["path"], d["line"], d["col"], d["name"], d["target"]): d
        for d in found
    }
    return sorted(
        unique.values(),
        key=lambda d: (d["path"], d["line"] or 0, d["col"] or 0, d["kind"], d["name"]),
    )


# --- signature changes --------------------------------------------------------


# The receivers that bind a method: a call through anything else that passes
# `self` itself is an unbound call through the class (issue #2899).
_PY_BOUND_RECEIVERS = frozenset({cs.PY_KEYWORD_SELF, cs.PY_KEYWORD_CLS})


@lru_cache(maxsize=cs.DELTA_PARSED_SOURCES_KEPT)
def _python_tree(path: str, mtime_ns: int, size: int) -> Tree | None:
    """One parse per version of a source file, shared by every site in it."""
    # Local import: parser_loader pulls in the language grammars.
    from .parser_loader import load_parsers

    parser = load_parsers()[0].get(cs.SupportedLanguage.PYTHON)
    if parser is None:
        return None
    try:
        return parser.parse(Path(path).read_bytes())
    except OSError:
        return None


def _python_source_tree(path: Path) -> Tree | None:
    if path.suffix != cs.EXT_PY:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return _python_tree(str(path), stat.st_mtime_ns, stat.st_size)


def _node_text(node: Node) -> str:
    return (node.text or b"").decode(cs.ENCODING_UTF8, errors="replace")


def _function_of(root: Node, name: str, start_line: int) -> Node | None:
    """The def named `name` starting on `start_line` (its decorators' line,
    for a decorated def)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node.end_point[0] + 1 < start_line or node.start_point[0] + 1 > start_line:
            continue
        if node.type == cs.TS_PY_FUNCTION_DEFINITION:
            name_node = node.child_by_field_name(cs.TS_FIELD_NAME)
            parent = node.parent
            starts = {node.start_point[0] + 1}
            if parent is not None and parent.type == cs.TS_PY_DECORATED_DEFINITION:
                starts.add(parent.start_point[0] + 1)
            if (
                name_node is not None
                and _node_text(name_node) == name
                and start_line in starts
            ):
                return node
        stack.extend(node.named_children)
    return None


def _parameters_of(root: Node, name: str, start_line: int) -> Node | None:
    function = _function_of(root, name, start_line)
    return function.child_by_field_name(cs.TS_FIELD_PARAMETERS) if function else None


def _is_star_args(parameter: Node) -> bool:
    # `*rest` or `*rest: int`; a bare `*` is a keyword separator, and
    # `**opts` takes keywords, not positionals.
    if parameter.type == cs.TS_PY_LIST_SPLAT_PATTERN:
        return True
    return parameter.type == cs.TS_PY_TYPED_PARAMETER and any(
        child.type == cs.TS_PY_LIST_SPLAT_PATTERN for child in parameter.named_children
    )


def _is_variadic(definition: Definition, repo_root: Path | None) -> bool:
    """Whether the Python definition's header declares `*args`.

    `positional_params` ends at the star (CPython counts nothing after it),
    so the stored list alone cannot tell `f(a)` from `f(a, *rest)`; the
    header is read back so a variadic callee is never reported as
    receiving too many arguments. Read from the syntax tree, not the text up
    to the first `)`, which a default such as `b=dict()` closes early, so
    `*rest` after it was never seen (issue #2899).
    """
    if repo_root is None or not definition.path or definition.start_line < 1:
        return False
    tree = _python_source_tree(repo_root / definition.path)
    if tree is None:
        return False
    parameters = _parameters_of(tree.root_node, definition.name, definition.start_line)
    return parameters is not None and any(
        _is_star_args(parameter) for parameter in parameters.named_children
    )


class _PySignature(NamedTuple):
    """A Python def's header, as a caller must satisfy it (issues #2853, #2845)."""

    positional: tuple[str, ...]
    positional_only: frozenset[str]
    keyword_only: tuple[str, ...]
    required: frozenset[str]
    var_positional: bool
    var_keyword: bool
    # Defined in a class and not a staticmethod: a bound call fills the
    # first positional (`self` / `cls`) itself.
    receiver: bool


def _decorators(function: Node) -> list[str]:
    parent = function.parent
    if parent is None or parent.type != cs.TS_PY_DECORATED_DEFINITION:
        return []
    return [
        _node_text(child).lstrip(cs.DECORATOR_AT).strip()
        for child in parent.named_children
        if child.type == cs.TS_PY_DECORATOR
    ]


def _parameter_name(parameter: Node) -> tuple[str, bool] | None:
    """(name, has a default) of a plain parameter, or None for anything else."""
    match parameter.type:
        case cs.TS_PY_IDENTIFIER:
            return _node_text(parameter), False
        case cs.TS_PY_DEFAULT_PARAMETER | cs.TS_PY_TYPED_DEFAULT_PARAMETER:
            name = parameter.child_by_field_name(cs.TS_FIELD_NAME)
            return (_node_text(name), True) if name is not None else None
        case cs.TS_PY_TYPED_PARAMETER:
            name = next(
                (c for c in parameter.named_children if c.type == cs.TS_PY_IDENTIFIER),
                None,
            )
            return (_node_text(name), False) if name is not None else None
    return None


def _is_star_kwargs(parameter: Node) -> bool:
    if parameter.type == cs.TS_PY_DICTIONARY_SPLAT_PATTERN:
        return True
    return parameter.type == cs.TS_PY_TYPED_PARAMETER and any(
        child.type == cs.TS_PY_DICTIONARY_SPLAT_PATTERN
        for child in parameter.named_children
    )


def _python_signature(
    definition: Definition, repo_root: Path | None
) -> _PySignature | None:
    """The header of a Python def, read back from its syntax tree.

    None when it cannot be read, or when a decorator other than the
    signature-preserving ones wraps it: `@click.command`, `@overload` or a
    fixture may stand between the call and the header, so judging the call
    against the header would be a guess.
    """
    if repo_root is None or not definition.path or definition.start_line < 1:
        return None
    tree = _python_source_tree(repo_root / definition.path)
    function = (
        _function_of(tree.root_node, definition.name, definition.start_line)
        if tree
        else None
    )
    parameters = (
        function.child_by_field_name(cs.TS_FIELD_PARAMETERS) if function else None
    )
    if function is None or parameters is None:
        return None
    decorators = _decorators(function)
    if any(d not in cs.PY_SIGNATURE_PRESERVING_DECORATORS for d in decorators):
        return None
    positional: list[str] = []
    keyword_only: list[str] = []
    positional_only: set[str] = set()
    required: set[str] = set()
    var_positional = var_keyword = after_star = False
    for parameter in parameters.named_children:
        if parameter.type == cs.TS_PY_POSITIONAL_SEPARATOR:
            positional_only.update(positional)
        elif parameter.type == cs.TS_PY_KEYWORD_SEPARATOR:
            after_star = True
        elif _is_star_args(parameter):
            var_positional = after_star = True
        elif _is_star_kwargs(parameter):
            var_keyword = True
        elif (named := _parameter_name(parameter)) is not None:
            name, has_default = named
            (keyword_only if after_star else positional).append(name)
            if not has_default:
                required.add(name)
    container = function.parent
    if container is not None and container.type == cs.TS_PY_DECORATED_DEFINITION:
        container = container.parent
    in_class = (
        container is not None
        and container.type == cs.TS_PY_BLOCK
        and container.parent is not None
        and container.parent.type == cs.TS_PY_CLASS_DEFINITION
    )
    return _PySignature(
        positional=tuple(positional),
        positional_only=frozenset(positional_only),
        keyword_only=tuple(keyword_only),
        required=frozenset(required),
        var_positional=var_positional,
        var_keyword=var_keyword,
        receiver=in_class and cs.PY_STATICMETHOD not in decorators,
    )


def _signature_verdict(
    site: CallSite, signature: _PySignature, bound: bool
) -> str | None:
    """A definite verdict the header alone settles, or None.

    `unexpected_keyword`: a keyword naming nothing the def accepts by
    keyword (a positional-only name included), with no `**kwargs` to take
    it, is a certain `TypeError` (issue #2853). `too_few`: a required
    parameter no positional and no keyword fills is one too, unless a
    `*rest` or `**opts` at the site may carry it (issue #2845).
    """
    positional = (
        signature.positional[1:]
        if bound and signature.receiver and signature.positional
        else signature.positional
    )
    by_keyword = {
        name for name in positional if name not in signature.positional_only
    } | set(signature.keyword_only)
    if not signature.var_keyword and any(
        name not in by_keyword for name in site.kwarg_names
    ):
        return cs.DELTA_ARITY_UNEXPECTED_KEYWORD
    if site.star_args or site.star_kwargs or site.arg_count is None:
        return None
    written = site.arg_count - len(site.kwarg_names)
    # Only a keyword the def accepts by name fills a parameter: one naming a
    # positional-only parameter lands in `**kw` and leaves it unfilled
    # (Greptile, PR #2947).
    supplied = set(positional[:written]) | (set(site.kwarg_names) & by_keyword)
    expected = (set(positional) | set(signature.keyword_only)) & signature.required
    return cs.DELTA_ARITY_TOO_FEW if expected - supplied else None


def _call_starting_at(root: Node, row: int, col: int) -> Node | None:
    node: Node | None = root.descendant_for_point_range((row, col), (row, col))
    while node is not None and node.start_point == (row, col):
        if node.type == cs.TS_PY_CALL:
            return node
        node = node.parent
    return None


def _passes_self_explicitly(site: CallSite, repo_root: Path | None) -> bool:
    """`Base.__init__(self, a)`: a method called through its class, with the
    receiver written as the first argument rather than bound (issue #2899).

    The site records only its counts, so the call is read back: a receiver
    other than `self`, `cls` or a call (`super()`), and a first positional
    argument that is `self`.
    """
    if repo_root is None or site.line is None or site.col is None:
        return False
    tree = _python_source_tree(repo_root / site.caller_path)
    if tree is None:
        return False
    call = _call_starting_at(tree.root_node, site.line - 1, site.col)
    function = call.child_by_field_name(cs.TS_FIELD_FUNCTION) if call else None
    if function is None or function.type != cs.TS_PY_ATTRIBUTE:
        return False
    receiver = function.child_by_field_name(cs.TS_FIELD_OBJECT)
    if (
        receiver is None
        or receiver.type == cs.TS_PY_CALL
        or _node_text(receiver) in _PY_BOUND_RECEIVERS
    ):
        return False
    arguments = call.child_by_field_name(cs.TS_FIELD_ARGUMENTS) if call else None
    first = next(
        (
            argument
            for argument in (arguments.named_children if arguments else [])
            if argument.type not in (cs.TS_PY_KEYWORD_ARGUMENT, cs.TS_COMMENT)
        ),
        None,
    )
    return (
        first is not None
        and first.type == cs.TS_PY_IDENTIFIER
        and _node_text(first) == cs.PY_KEYWORD_SELF
    )


# Bindings that name the function the call runs. Absent is the legacy exact.
_DEFINITE_RESOLUTIONS = frozenset(
    {"", cs.EdgeResolution.EXACT.value, cs.EdgeResolution.TRACE_CONFIRMED.value}
)


def _is_receiver(entry: str, language: cs.SupportedLanguage | None) -> bool:
    """Whether `entry` is the receiver marker of the definition's language.

    Only Rust writes `self` and only C# writes `this name`: elsewhere, or
    where the language is unknown, `self` is an ordinary parameter stored as
    written, and counting it out would shrink the bounds by one.
    """
    if language == cs.SupportedLanguage.RUST:
        return entry == cs.POSITIONAL_RECEIVER_SELF
    if language == cs.SupportedLanguage.CSHARP:
        return entry.startswith(cs.POSITIONAL_RECEIVER_THIS_PREFIX)
    return False


class _DeclaredBounds(NamedTuple):
    """A declared list as counts: what a call must and may pass."""

    receiver: bool
    fixed: int
    fewest: int
    most: int | None


def _declared_bounds(
    declared: tuple[str, ...], language: cs.SupportedLanguage | None
) -> _DeclaredBounds:
    """The counts of the parameters a call fills, the receiver left out.

    A default before a required parameter still has to be passed to reach
    it, so the fewest runs to the last required one; a rest parameter
    lifts the most.
    """
    receiver = bool(declared) and _is_receiver(declared[0], language)
    params = declared[1:] if receiver else declared
    fixed = [p for p in params if not p.startswith(cs.POSITIONAL_REST_PREFIX)]
    fewest = max(
        (
            index + 1
            for index, entry in enumerate(fixed)
            if not entry.endswith(cs.POSITIONAL_OPTIONAL_SUFFIX)
        ),
        default=0,
    )
    most = None if len(fixed) < len(params) else len(fixed)
    return _DeclaredBounds(receiver, len(fixed), fewest, most)


def _declaring_type(definition: Definition) -> str:
    # `proj.Util.Util.Ext(string, int)`: a C# method's qn ends in its
    # parameter types, which may hold dots of their own.
    qualified = definition.qualified_name.partition(cs.CHAR_PAREN_OPEN)[0]
    segments = qualified.split(cs.SEPARATOR_DOT)
    return segments[-2] if len(segments) > 1 else ""


def _receiver_passed(site: CallSite, definition: Definition) -> bool | None:
    """Whether the call puts the receiver in its argument list; None if the
    written call does not say.

    Rust passes it on a path call (`S::m(s, 1)`, `Self::m(self)`) and never
    on `s.m(1)`. A C# extension method takes it as the first argument when
    called bare under `using static` or through its own class
    (`Util.Ext(s, 1)`), and from a value otherwise: ingestion records ""
    for a local, parameter, field or property of that name. Another name
    binds nothing visible at the call (an inherited member, an alias), so
    the call form stays undecided.
    """
    qualifier = site.call_qualifier
    if _language(definition.path) == cs.SupportedLanguage.CSHARP:
        if qualifier is None or qualifier == _declaring_type(definition):
            return True
        return False if qualifier == "" else None
    return bool(qualifier) if qualifier is not None else None


def _filled_counts(
    passed: int, site: CallSite, definition: Definition, receiver: bool
) -> tuple[int, int]:
    """The parameters the site fills, once per call form it may be.

    Always a pair: a site whose form is known repeats its one count, which
    the caller's set of verdicts collapses.
    """
    if not receiver:
        return passed, passed
    explicit = _receiver_passed(site, definition)
    if explicit is None:
        return passed, passed - 1
    filled = passed - 1 if explicit else passed
    return filled, filled


def _rejected(
    rule: frozenset[cs.SupportedLanguage], site: CallSite, definition: Definition
) -> bool:
    # Both ends: nothing checks a JavaScript caller of a TypeScript callee.
    return _language(site.caller_path) in rule and _language(definition.path) in rule


def _count_verdict(
    filled: int, bounds: _DeclaredBounds, site: CallSite, definition: Definition
) -> str:
    if filled >= bounds.fewest and (bounds.most is None or filled <= bounds.most):
        return cs.DELTA_ARITY_OK
    # Bound by name alone, the edge may lead to a same-named function the
    # call never runs, whose count proves nothing about this one.
    if site.resolution not in _DEFINITE_RESOLUTIONS:
        return cs.DELTA_ARITY_UNKNOWN
    if filled < bounds.fewest:
        if _rejected(cs.ARITY_REJECTS_MISSING, site, definition):
            return cs.DELTA_ARITY_TOO_FEW
        return cs.DELTA_ARITY_POSSIBLY_MISSING
    if _rejected(cs.ARITY_REJECTS_SURPLUS, site, definition):
        return cs.DELTA_ARITY_TOO_MANY
    return cs.DELTA_ARITY_OK


def _declared_arity_verdict(site: CallSite, definition: Definition) -> tuple[int, str]:
    """A site's verdict against a signature that declares its optionality.

    Unlike Python's, these lists say which parameters may be left out, so
    fewer arguments than the required ones is a finding where the language
    rejects the call (issue #2517). A call that may or may not pass the
    receiver is judged both ways and keeps a verdict only if they agree.
    """
    declared = definition.positional_params
    if declared is None:
        return -1, cs.DELTA_ARITY_UNKNOWN
    bounds = _declared_bounds(declared, _language(definition.path))
    passed = site.arg_count
    if passed is None or site.spread_args:
        return bounds.fixed, cs.DELTA_ARITY_UNKNOWN
    verdicts = {
        _count_verdict(filled, bounds, site, definition)
        for filled in _filled_counts(passed, site, definition, bounds.receiver)
    }
    return bounds.fixed, (
        verdicts.pop() if len(verdicts) == 1 else cs.DELTA_ARITY_UNKNOWN
    )


def _arity_verdict(
    site: CallSite, definition: Definition, repo_root: Path | None
) -> tuple[int, str]:
    if _language(definition.path) in cs.DECLARED_ARITY_LANGUAGES:
        return _declared_arity_verdict(site, definition)
    declared_count, verdict = _python_count_verdict(site, definition, repo_root)
    if verdict == cs.DELTA_ARITY_TOO_MANY:
        return declared_count, verdict
    signature = _python_signature(definition, repo_root)
    if signature is None:
        return declared_count, verdict
    bound = definition.label in _METHOD_LABELS and not (
        (
            site.arg_count is not None
            and site.arg_count > len(site.kwarg_names)
            and _passes_self_explicitly(site, repo_root)
        )
        or _passes_receiver_by_keyword(site, signature)
    )
    return declared_count, (_signature_verdict(site, signature, bound) or verdict)


def _passes_receiver_by_keyword(site: CallSite, signature: _PySignature) -> bool:
    """`Base.m(self=self, x=x)`: the receiver supplied by keyword.

    A bound call naming its own receiver is a `TypeError` (multiple values
    for it), so a site that names it is an unbound call through the class
    (Greptile, PR #2947).
    """
    return (
        signature.receiver
        and bool(signature.positional)
        and signature.positional[0] in site.kwarg_names
    )


def _python_count_verdict(
    site: CallSite, definition: Definition, repo_root: Path | None
) -> tuple[int, str]:
    declared = definition.positional_params
    if declared is None:
        return -1, cs.DELTA_ARITY_UNKNOWN
    if site.arg_count is None:
        return len(declared), cs.DELTA_ARITY_UNKNOWN
    # An unbound call through the class supplies `self` itself, so it is
    # judged as a plain function call against every declared parameter.
    is_method = definition.label in _METHOD_LABELS and not (
        site.arg_count > len(site.kwarg_names)
        and _passes_self_explicitly(site, repo_root)
    )
    # `arg_count` counts keyword arguments too (issue #1522); only the
    # positionals plus the keywords naming a declared positional parameter
    # fill the declared list. A keyword naming nothing declared is neutral:
    # it may be a keyword-only parameter or `**kwargs`, which the stored
    # positional list cannot see, and a wrong name is not an arity fault.
    positional = site.arg_count - len(site.kwarg_names)
    matched = sum(1 for name in site.kwarg_names if name in declared)
    passed = positional + matched + (1 if is_method else 0)
    # `diagnose_arity` owns the receiver arithmetic (`self` counts for
    # CPython but is not caller-supplied); here the "message" is the site.
    verdict = diagnose_arity(
        ArityError(callee=definition.name, expected=passed, actual=passed),
        declared,
        is_method,
    )
    declared_count = verdict.declared_count - (1 if is_method else 0)
    if positional + (1 if is_method else 0) > verdict.declared_count:
        if _is_variadic(definition, repo_root):
            return declared_count, cs.DELTA_ARITY_OK
        return declared_count, cs.DELTA_ARITY_TOO_MANY
    # `*rest` adds positionals the graph cannot count, so the written ones
    # are a floor: over the declared list they are too many whatever `rest`
    # holds (above), but at or under it nothing says whether the call fits
    # (issue #2635). `**opts` adds no positional and needs no such care.
    if site.star_args:
        return declared_count, cs.DELTA_ARITY_UNKNOWN
    if verdict.confirmed:
        return declared_count, cs.DELTA_ARITY_OK
    if passed > verdict.declared_count:
        return declared_count, cs.DELTA_ARITY_OK
    return declared_count, cs.DELTA_ARITY_POSSIBLY_MISSING


def _site_finding(
    site: CallSite, definition: Definition, repo_root: Path | None
) -> ArityAtSite:
    declared_count, verdict = _arity_verdict(site, definition, repo_root)
    if site.resolution in cs.DELTA_GUESSED_RESOLUTIONS:
        # The callee is a guess from the name alone, or one of several
        # same-named candidates: the site may not call it at all, so its
        # arity says nothing certain (issue #2639).
        verdict = cs.DELTA_ARITY_UNKNOWN
    return ArityAtSite(
        caller=site.caller,
        path=site.caller_path,
        line=site.line,
        col=site.col,
        arg_count=site.arg_count,
        kwarg_names=list(site.kwarg_names),
        declared_count=declared_count,
        verdict=verdict,
        callee=site.callee,
        resolution=site.resolution or cs.EdgeResolution.EXACT.value,
    )


def _site_order(finding: ArityAtSite) -> tuple[str, int, int]:
    return (finding["path"], finding["line"] or 0, finding["col"] or 0)


def _arity_findings(after: Snapshot, repo_root: Path | None) -> list[ArityAtSite]:
    """Call sites in the re-parsed files the callee's language rejects.

    The definitive verdicts only: a Python site passing fewer positional
    arguments may be relying on defaults the graph does not record, so only
    a signature that declares its optionality yields `too_few`.
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
        if finding["verdict"] in cs.DELTA_ARITY_DEFINITE:
            out.append(finding)
    return sorted(out, key=_site_order)


def _remote_callers(
    fetch_all: QueryFn, project_name: str, handlers: list[str]
) -> dict[str, list[RemoteCaller]]:
    """Per changed definition, the call sites in OTHER projects reaching an
    endpoint it exposes; one read per shape, only when something changed."""
    found: dict[str, list[RemoteCaller]] = {}
    if not handlers:
        return found
    # Each query is DISTINCT within itself; a caller the two shapes both
    # return is one caller (bot review on PR #1978).
    seen: set[tuple[str, str, str, str, str, str]] = set()
    for query in (
        cq.CYPHER_DELTA_REMOTE_CALLERS_OF,
        cq.CYPHER_DELTA_REMOTE_DIRECT_CALLERS_OF,
    ):
        for row in fetch_all(
            query,
            {cs.KEY_QNS: handlers, cs.KEY_PROJECT_PREFIX: _prefix(project_name)},
        ):
            handler = _text(row.get(cs.KEY_HANDLER))
            caller = RemoteCaller(
                qualified_name=_text(row.get(cs.KEY_QUALIFIED_NAME)),
                label=_text(row.get(cs.KEY_LABEL)),
                path=_text(row.get(cs.KEY_PATH)),
                url=_text(row.get(cs.KEY_URL)),
                endpoint=_text(row.get(cs.KEY_ENDPOINT)),
            )
            key = (
                handler,
                caller["qualified_name"],
                caller["label"],
                caller["path"],
                caller["url"],
                caller["endpoint"],
            )
            if handler and key not in seen:
                seen.add(key)
                found.setdefault(handler, []).append(caller)
    for rows in found.values():
        rows.sort(key=lambda r: (r["qualified_name"], r["url"], r["path"]))
    return found


def _signature_changes(
    before: Snapshot,
    after: Snapshot,
    symbols: SymbolDelta,
    repo_root: Path | None,
    fetch_all: QueryFn | None = None,
    project_name: str = "",
) -> list[SignatureChange]:
    out: list[SignatureChange] = []
    changed = [
        qn
        for qn in symbols["changed"]
        if _params_moved(before.definitions[qn], after.definitions[qn])
        or before.definitions[qn].is_async != after.definitions[qn].is_async
    ]
    # A handler that only turned `async` or back serves the same route with
    # the same parameters, so its remote callers are not broken by the flip
    # (Greptile, PR #2951); only a parameter change is looked up for them.
    remote = (
        _remote_callers(
            fetch_all,
            project_name,
            [
                qn
                for qn in changed
                if _params_moved(before.definitions[qn], after.definitions[qn])
            ],
        )
        if fetch_all is not None
        else {}
    )
    for qn in changed:
        old, new = before.definitions[qn], after.definitions[qn]
        async_change = _async_change(old, new)
        sites = [
            _flipped_site(
                _site_finding(site, new, repo_root),
                new,
                async_change,
                _site_call(site, repo_root),
            )
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
                async_change=async_change,
                remote_callers=remote.get(qn, []),
            )
        )
    return out


# Nodes that use a call's value where it stands, so a coroutine there is
# never run (issue #2860); an attribute or subscript reads its object.
_PY_SYNC_VALUE_READERS = frozenset(
    {
        cs.TS_PY_EXPRESSION_STATEMENT,
        cs.TS_PY_BINARY_OPERATOR,
        cs.TS_PY_COMPARISON_OPERATOR,
        cs.TS_PY_BOOLEAN_OPERATOR,
        cs.TS_PY_NOT_OPERATOR,
        cs.TS_PY_UNARY_OPERATOR,
    }
)
_PY_READ_OBJECT_FIELDS = {
    cs.TS_PY_ATTRIBUTE: cs.TS_FIELD_OBJECT,
    cs.TS_PY_SUBSCRIPT: cs.FIELD_VALUE,
}


def _async_change(old: Definition, new: Definition) -> str | None:
    if old.is_async == new.is_async:
        return None
    return cs.DELTA_ASYNC_ADDED if new.is_async else cs.DELTA_ASYNC_REMOVED


def _flipped_site(
    finding: ArityAtSite,
    definition: Definition,
    async_change: str | None,
    call: Node | None,
) -> ArityAtSite:
    # A call written for the old kind of a Python def fails (issue #2860):
    # its value read synchronously once the def turned `async` (a coroutine
    # whose body never runs), or awaited once it stopped being so. How the
    # call is written decides it, so a caller already migrated is fine and
    # one handing the value on (returned, assigned, `asyncio.run(...)`) is
    # left to the change's `async_change` hint (Greptile, PR #2951). An
    # arity verdict that already fails is the more precise word. Elsewhere
    # (a JS Promise still runs) the flip is listed, not judged.
    if (
        async_change is None
        or call is None
        or Path(definition.path).suffix != cs.EXT_PY
        or finding["verdict"] in cs.DELTA_ARITY_DEFINITE
        or finding["resolution"] in cs.DELTA_GUESSED_RESOLUTIONS
    ):
        return finding
    parent, child = _value_parent(call)
    awaited = parent is not None and parent.type == cs.TS_PY_AWAIT
    if async_change == cs.DELTA_ASYNC_ADDED:
        breaks = not awaited and _read_synchronously(parent, child)
    else:
        breaks = awaited
    if not breaks:
        return finding
    flipped = finding.copy()
    flipped["verdict"] = cs.DELTA_ARITY_ASYNC_CHANGED
    return flipped


def _site_call(site: CallSite, repo_root: Path | None) -> Node | None:
    """The call a Python site records, read back from its source, or None."""
    if (
        repo_root is None
        or site.line is None
        or site.col is None
        or Path(site.caller_path).suffix != cs.EXT_PY
    ):
        return None
    tree = _python_source_tree(repo_root / site.caller_path)
    if tree is None:
        return None
    return _call_starting_at(tree.root_node, site.line - 1, site.col)


def _value_parent(call: Node) -> tuple[Node | None, Node]:
    """The node that receives a call's value, past any parentheses, and the
    child of it the value arrives through."""
    child = call
    parent = call.parent
    while parent is not None and parent.type == cs.TS_PY_PARENTHESIZED_EXPRESSION:
        child, parent = parent, parent.parent
    return parent, child


def _read_synchronously(parent: Node | None, child: Node) -> bool:
    """Whether the value is used on the spot: discarded as a statement, an
    operand, or the object of an attribute or subscript read."""
    if parent is None:
        return False
    if parent.type in _PY_SYNC_VALUE_READERS:
        return True
    field = _PY_READ_OBJECT_FIELDS.get(parent.type)
    target = parent.child_by_field_name(field) if field else None
    return target is not None and target.id == child.id


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


def _match_duplicate_shapes(
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


def _best_duplicate_match(
    candidate: _Shape,
    shapes: list[_Shape],
    fresh_set: set[str],
    threshold: float,
) -> tuple[float, _Shape, str] | None:
    """The most similar prior shape `candidate` duplicates, if any."""
    best: tuple[float, _Shape, str] | None = None
    for other in shapes:
        found = _match_duplicate_shapes(candidate, other, threshold)
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
    return best


def _new_duplicate(
    candidate: _Shape, other: _Shape, kind: str, similarity: float
) -> NewDuplicate:
    return NewDuplicate(
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


def _new_duplicates(
    fetch_all: QueryFn,
    project_name: str,
    fresh: Iterable[str],
    longer_project_prefixes: tuple[str, ...] = (),
    threshold: float = cs.DUPLICATES_DEFAULT_THRESHOLD,
    min_nodes: int = cs.DUPLICATES_DEFAULT_MIN_NODES,
) -> list[NewDuplicate]:
    fresh_set = set(fresh)
    if not fresh_set:
        return []
    rows = [
        row
        for row in fetch_all(
            cq.CYPHER_DUPLICATE_FINGERPRINTS,
            {
                cs.KEY_PROJECT_PREFIX: _prefix(project_name),
                cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_project_prefixes),
            },
        )
        if not _has_longer_project_owner(
            _text(row.get(cs.KEY_QUALIFIED_NAME)), longer_project_prefixes
        )
    ]
    shapes = [
        s for s in (_shape(r) for r in rows) if s.fingerprint and s.nodes >= min_nodes
    ]
    out: list[NewDuplicate] = []
    for candidate in shapes:
        if candidate.qualified_name not in fresh_set:
            continue
        best = _best_duplicate_match(candidate, shapes, fresh_set, threshold)
        if best is not None:
            similarity, other, kind = best
            out.append(_new_duplicate(candidate, other, kind, similarity))
    return sorted(out, key=lambda d: (d["path"], d["start_line"], d["qualified_name"]))


# --- import cycles ------------------------------------------------------------


class _TarjanState:
    """The bookkeeping Tarjan's algorithm carries between its two phases."""

    def __init__(self) -> None:
        self.index: dict[str, int] = {}
        self.low: dict[str, int] = {}
        self.on_stack: set[str] = set()
        self.stack: list[str] = []
        self.counter = 0

    def enter(self, node: str) -> None:
        """Number `node` and push it onto the current path."""
        self.index[node] = self.low[node] = self.counter
        self.counter += 1
        self.stack.append(node)
        self.on_stack.add(node)

    def pop_component(self, root: str) -> frozenset[str]:
        """Unwind the path down to `root`, which closes one SCC."""
        component: set[str] = set()
        while True:
            member = self.stack.pop()
            self.on_stack.discard(member)
            component.add(member)
            if member == root:
                return frozenset(component)


def _tarjan_descend(
    state: _TarjanState,
    graph: dict[str, frozenset[str]],
    work: list[tuple[str, Iterator[str]]],
    node: str,
    child: str,
) -> None:
    """Follow one edge: recurse into an unseen child, else relax the low-link."""
    if child not in state.index:
        state.enter(child)
        work.append((child, iter(sorted(graph.get(child, ())))))
    elif child in state.on_stack:
        state.low[node] = min(state.low[node], state.index[child])


def strongly_connected(graph: dict[str, frozenset[str]]) -> list[frozenset[str]]:
    """Tarjan's SCCs, iteratively (a module graph can be thousands deep)."""
    state = _TarjanState()
    out: list[frozenset[str]] = []
    for root in sorted(graph):
        if root in state.index:
            continue
        work: list[tuple[str, Iterator[str]]] = [
            (root, iter(sorted(graph.get(root, ()))))
        ]
        state.enter(root)
        while work:
            node, children = work[-1]
            child = next(children, None)
            if child is not None:
                _tarjan_descend(state, graph, work, node, child)
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                state.low[parent] = min(state.low[parent], state.low[node])
            if state.low[node] == state.index[node]:
                out.append(state.pop_component(node))
    return out


def _cycles(graph: dict[str, frozenset[str]]) -> set[frozenset[str]]:
    return {
        component
        for component in strongly_connected(graph)
        if len(component) > 1
        or any(member in graph.get(member, ()) for member in component)
    }


def import_cycles(graph: dict[str, frozenset[str]]) -> set[frozenset[str]]:
    """The cyclic strongly connected components of a module import graph."""
    return _cycles(graph)


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


def _walk_callers(
    fetch_all: QueryFn,
    prefix: str,
    targets: set[str],
    longer_project_prefixes: tuple[str, ...],
) -> _Reach:
    """Multi-source backward BFS, one indexed query per hop.

    The cost is proportional to what is reached rather than to the project:
    a project-wide reverse call graph costs more to build than most deltas
    take in total.
    """
    depth = dict.fromkeys(targets, 0)
    through = {qn: qn for qn in targets}
    nodes: dict[_NodeId, PropertyDict] = {}
    frontier = sorted(targets)
    for hop in range(1, cs.DELTA_REACH_MAX_DEPTH + 1):
        if not frontier:
            break
        rows = fetch_all(
            cq.CYPHER_DELTA_CALLERS_OF,
            {
                cs.KEY_PROJECT_PREFIX: prefix,
                cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_project_prefixes),
                cs.KEY_QNS: frontier,
            },
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
    fetch_all: QueryFn,
    prefix: str,
    reached: dict[_NodeId, PropertyDict],
    longer_project_prefixes: tuple[str, ...],
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
    for row in fetch_all(
        cq.CYPHER_DELTA_RUST_MODULES,
        {
            cs.KEY_PROJECT_PREFIX: prefix,
            cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_project_prefixes),
        },
    ):
        qn = _text(row.get(cs.KEY_QUALIFIED_NAME))
        if qn:
            modules[(_text(row.get(cs.KEY_LABEL)), qn)] = _node_props(row)
    functions: dict[_NodeId, PropertyDict] = {}
    for row in fetch_all(
        cq.CYPHER_DELTA_RUST_TEST_FNS,
        {
            cs.KEY_PROJECT_PREFIX: prefix,
            cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_project_prefixes),
            cs.CYPHER_PARAM_PATHS: rust_paths,
        },
    ):
        qn = _text(row.get(cs.KEY_QUALIFIED_NAME))
        if qn:
            functions[(_text(row.get(cs.KEY_LABEL)), qn)] = _node_props(row)
    return _rust_test_modules_from_nodes(modules), _rust_test_fn_spans(functions)


def _tests_reaching(
    fetch_all: QueryFn,
    project_name: str,
    targets: Iterable[str],
    longer_project_prefixes: tuple[str, ...] | None = None,
) -> list[TestReach]:
    prefix = _prefix(project_name)
    longer_prefixes = (
        _longer_project_prefixes(fetch_all, project_name)
        if longer_project_prefixes is None
        else longer_project_prefixes
    )
    reach = _walk_callers(fetch_all, prefix, set(targets), longer_prefixes)
    rust_modules, rust_spans = _rust_inputs(
        fetch_all, prefix, reach.nodes, longer_prefixes
    )
    out: list[TestReach] = []
    for (_label, raw_qn), props in reach.nodes.items():
        qn = str(raw_qn)
        path = str(props.get(cs.KEY_PATH) or "")
        if _is_test_symbol(props, qn, path, rust_modules, rust_spans):
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


def _inbound_calls(snap: Snapshot) -> int:
    return sum(
        1
        for site in snap.sites
        if site.rel == cs.RelationshipType.CALLS.value
        and site.callee in snap.definitions
    )


def structural_delta(
    fetch_all: QueryFn,
    project_name: str,
    before: Snapshot,
    after: Snapshot,
    report: ReingestReport | None = None,
    repo_root: Path | None = None,
    declared_renames: frozenset[tuple[str, str]] = frozenset(),
    *,
    longer_project_prefixes: tuple[str, ...] | None = None,
) -> StructuralDelta:
    """Diff two snapshots of the same paths, then look up what they touch.

    `declared_renames` are pairs the CALLER applied and therefore knows. They
    are needed only where the snapshots cannot show identity -- an empty
    container -- and are empty for a plain write, which really is inferring.
    """
    started = time.perf_counter()
    longer_prefixes = (
        _longer_project_prefixes(fetch_all, project_name)
        if longer_project_prefixes is None
        else longer_project_prefixes
    )
    symbols = _symbols(before, after, declared_renames)
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
        dangling_importers=_dangling_importers(
            before,
            after,
            symbols,
            repo_root,
            _module_loader(
                fetch_all,
                {
                    cs.KEY_PROJECT_PREFIX: _prefix(project_name),
                    cs.KEY_LONGER_PROJECT_PREFIXES: list(longer_prefixes),
                },
            ),
        ),
        signature_changes=_signature_changes(
            before, after, symbols, repo_root, fetch_all, project_name
        ),
        arity_findings=_arity_findings(after, repo_root),
        new_duplicates=_new_duplicates(fetch_all, project_name, fresh, longer_prefixes),
        new_import_cycles=_new_import_cycles(before, after),
        stale_importers=_stale_importers(after, symbols),
        tests_reaching=_tests_reaching(
            fetch_all, project_name, touched, longer_prefixes
        )
        if touched
        else [],
        call_sites=SiteCounts(
            before=_inbound_calls(before), after=_inbound_calls(after)
        ),
        reingest_ms=round(report.elapsed_ms, 1) if report else 0.0,
        delta_ms=round((time.perf_counter() - started) * 1000, 1),
    )


def _module_of(qualified_name: str) -> str:
    """The module a symbol's qualified name sits in, or "" if it has none."""
    head, sep, _tail = qualified_name.rpartition(cs.SEPARATOR_DOT)
    return head if sep else ""


def _stale_importers(after: Snapshot, symbols: SymbolDelta) -> list[StaleImporter]:
    """Importers still naming a module every moved symbol has left.

    A MOVE is a rename whose module segment changed; a plain rename keeps the
    module and is not one, so it contributes no vacated module and this
    returns empty for it. That is what keeps the check specific to moves
    without the contract having to say so (#1825).

    "Vacated" is the load-bearing word. A module that still defines anything
    is a legitimate import target, so only a module the moved symbols left
    EMPTY counts -- otherwise moving one helper out of a busy module would
    report every importer of its remaining siblings.
    """
    vacated: set[str] = set()
    for renamed in symbols["renamed"]:
        old_module = _module_of(str(renamed["old"]))
        new_module = _module_of(str(renamed["new"]))
        if old_module and old_module != new_module:
            vacated.add(old_module)
    if not vacated:
        return []
    # Anything the graph still defines under a vacated module means the module
    # is alive and importing it is correct.
    still_defined = {
        _module_of(qn) for qn in after.definitions if _module_of(qn) in vacated
    }
    vacated -= still_defined
    if not vacated:
        return []
    stale: list[StaleImporter] = []
    for importer, targets in after.imports.items():
        # One entry per IMPORTER, not per stale target: the finding is that
        # this module still points at somewhere the move emptied, and naming
        # the same importer once per vacated target would repeat it.
        if not targets & vacated:
            continue
        stale.append(
            StaleImporter(
                importer=importer,
                path=after.module_paths.get(importer, ""),
                line=0,
            )
        )
    return sorted(stale, key=lambda entry: (entry["path"], entry["importer"]))


def observe(
    fetch_all: QueryFn,
    project_name: str,
    paths: Iterable[str],
    apply: Callable[[], ReingestReport],
    repo_root: Path | None = None,
    declared_renames: frozenset[tuple[str, str]] = frozenset(),
) -> StructuralDelta:
    """Snapshot `paths`, run `apply` (the scoped re-ingest), snapshot, diff.

    The caller holds whatever lock serialises graph writes; both reads and
    the re-ingest must see one generation of the graph.
    """
    path_list = sorted(set(paths))
    started = time.perf_counter()
    longer_prefixes = _longer_project_prefixes(fetch_all, project_name)
    before = snapshot(
        fetch_all,
        project_name,
        path_list,
        longer_project_prefixes=longer_prefixes,
    )
    apply_started = time.perf_counter()
    report = apply()
    reingest_ms = (time.perf_counter() - apply_started) * 1000
    after = snapshot(
        fetch_all,
        project_name,
        path_list,
        longer_project_prefixes=longer_prefixes,
    )
    delta = structural_delta(
        fetch_all,
        project_name,
        before,
        after,
        report,
        repo_root,
        declared_renames=declared_renames,
        longer_project_prefixes=longer_prefixes,
    )
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
        # An import of a removed name fails when the importing module loads,
        # with or without a call site to go with it (issue #2516).
        or delta["dangling_importers"]
        # Only a definite verdict: a `possibly_missing` site (`send(msg)`
        # becoming `send(msg, channel=None)`) is a hint the JSON keeps, not
        # a finding (issue #2656, as structural-delta.md documents). A
        # declared signature settles `too_few` (#2517); a Python header read
        # back settles `too_few` and `unexpected_keyword` (#2845, #2853).
        or any(
            site["verdict"] in cs.DELTA_ARITY_DEFINITE
            for change in delta["signature_changes"]
            for site in change["sites"]
        )
        # A changed handler reached from another service is a contract
        # change no local CALLS edge shows (issue #1603).
        or any(change["remote_callers"] for change in delta["signature_changes"])
        or delta["arity_findings"]
        or delta["new_duplicates"]
        or delta["new_import_cycles"]
    )
