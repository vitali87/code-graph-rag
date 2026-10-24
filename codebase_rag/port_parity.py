"""Cross-language parity between two implementations (`cgr port check`).

Every language lands in one schema, so a Python function and its Rust twin
are both callables with `CALLS` edges and `positional_params`, and two
implementations of the same code can be compared in the graph (epic #3413).
This module pairs the callables of an OLD and a NEW scope and judges each
pair on the dimensions the graph records for both sides: the first-party
calls it makes, mapped through the pairs, and its fixed positional arity.

Each dimension answers MATCH, DIFFERS or UNKNOWN, and UNKNOWN is never
reported as MATCH: a difference resting only on edges the resolver guessed
(`heuristic`, `overload`) may be the other side binding the same call
exactly, and a language that records no positional parameters cannot be
compared on arity. Pairs are never guessed either: a name several callables
of a side share, and no container tells apart, is reported as ambiguous with
its candidates, for `--pair` to settle.

The reads are the dead-code fetch (nodes and edges, the same rows and test
classification `tests_reaching` uses) and the structural delta's definitions
query for the positional parameters, so every backend answers them.
"""

import re
from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
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
from .graph_query import QueryFn, _longer_project_prefixes, _owner_check, _prefix
from .parsers.utils import _is_static_decorator
from .structural_delta import _is_receiver, _language
from .types_defs import PropertyDict, PropertyValue, ResultValue
from .utils.path_utils import portable_posix

_CAMEL_BOUNDARY = re.compile(cs.PORT_CAMEL_BOUNDARY_PATTERN)


class PortCheckError(ValueError):
    """A scope or `--pair` the graph cannot answer for, with the exit status
    the CLI reports it under."""

    def __init__(
        self, message: str, exit_code: int = cs.GRAPH_EXIT_UNKNOWN_TARGET
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class PortScope(NamedTuple):
    """One side of the comparison: a project and a path inside it, relative to
    its repository root (empty or `.` for the whole project)."""

    project: str
    scope: str


class SymbolRef(TypedDict):
    qualified_name: str
    path: str
    start_line: int | None


class CallDifference(TypedDict):
    # `resolution` is the edge's on the side that has the call.
    old: str
    new: str
    resolution: str | None


class CallsParity(TypedDict):
    verdict: cs.ParityVerdict
    missing: list[CallDifference]
    extra: list[CallDifference]
    unpaired_old: list[str]
    unpaired_new: list[str]


class ArityParity(TypedDict):
    # `old` and `new` count the fixed positional parameters, receivers left
    # out. `open` is true when a side declares parameters past them (a rest
    # parameter, or a Python `*args`, keyword-only or `**kwargs` one), which
    # the counts alone cannot compare.
    verdict: cs.ParityVerdict
    old: int | None
    new: int | None
    open: bool


class PairReport(TypedDict):
    old: SymbolRef
    new: SymbolRef
    paired_by: cs.PairedBy
    verdict: cs.ParityVerdict
    calls: CallsParity
    arity: ArityParity


class AmbiguousName(TypedDict):
    name: str
    old: list[SymbolRef]
    new: list[SymbolRef]


class SideSummary(TypedDict):
    project: str
    scope: str
    definitions: int


class PairedByCounts(TypedDict):
    explicit: int
    module: int
    container: int
    name: int


class ParitySummary(TypedDict):
    pairs: int
    paired_by: PairedByCounts
    match: int
    differs: int
    unknown: int
    old_only: int
    new_only: int
    ambiguous: int


class ParityReport(TypedDict):
    old: SideSummary
    new: SideSummary
    summary: ParitySummary
    pairs: list[PairReport]
    old_only: list[SymbolRef]
    new_only: list[SymbolRef]
    ambiguous: list[AmbiguousName]


class _Callable(NamedTuple):
    qualified_name: str
    label: str
    # Normalised name and container: what pairing compares.
    key: str
    container: str
    path: str
    start_line: int | None
    decorators: tuple[str, ...]
    # How many parameters the definition declares in all: `param_types` is
    # parallel to them. Read only for Python, whose `positional_params` stop
    # at `*args` and the keyword-only ones.
    declared: int
    # The module path relative to the side's scope, set once the scope is
    # known: `a/utils` tells `legacy/a/utils.py` from `legacy/b/utils.py`.
    module: str = ""


class _Arity(NamedTuple):
    fixed: int
    open: bool


class _ProjectGraph(NamedTuple):
    callables: dict[str, _Callable]
    tests: frozenset[str]
    # caller -> callee -> resolution, callables of the project only
    calls: dict[str, dict[str, str | None]]


class _Side(NamedTuple):
    spec: PortScope
    callables: dict[str, _Callable]
    calls: dict[str, dict[str, str | None]]
    positional: dict[str, tuple[str, ...]]


class _Pairing(NamedTuple):
    pairs: list[tuple[str, str, cs.PairedBy]]
    old_only: list[str]
    new_only: list[str]
    ambiguous: list[tuple[str, list[str], list[str]]]


def normalize_name(name: str) -> str:
    """Fold case convention and surrounding underscores, so `_read_number`,
    `readNumber` and `ReadNumber` compare equal."""
    core = name.strip(cs.PORT_NAME_STRIP_CHARS) or name
    return _CAMEL_BOUNDARY.sub(cs.PORT_NAME_JOIN, core).lower()


def parse_pairs(values: Iterable[str]) -> list[tuple[str, str]]:
    """`OLD=NEW` options as (old, new) targets."""
    pairs: list[tuple[str, str]] = []
    for value in values:
        old, sep, new = value.partition(cs.PORT_PAIR_SEPARATOR)
        if not sep or not old.strip() or not new.strip():
            raise PortCheckError(
                cs.PORT_ERR_PAIR_FORMAT.format(value=value), cs.PORT_EXIT_USAGE
            )
        pairs.append((old.strip(), new.strip()))
    return pairs


def has_differences(report: ParityReport) -> bool:
    return report["summary"]["differs"] > 0


def port_check(
    fetch_all: QueryFn,
    old: PortScope,
    new: PortScope,
    pairs: Sequence[tuple[str, str]] = (),
    include_tests: bool = False,
) -> ParityReport:
    """Pair the callables of `old` and `new` and judge every pair."""
    old_scope = _normalise_scope(old.scope)
    new_scope = _normalise_scope(new.scope)
    if old.project == new.project and _overlaps(old_scope, new_scope):
        raise PortCheckError(
            cs.PORT_ERR_OVERLAPPING_SCOPES.format(
                old=old.scope, new=new.scope, project=old.project
            ),
            cs.PORT_EXIT_USAGE,
        )
    graphs: dict[str, _ProjectGraph] = {}
    for project in (old.project, new.project):
        if project not in graphs:
            graphs[project] = _load_project(fetch_all, project)
    old_side = _side(
        fetch_all, graphs[old.project], PortScope(old.project, old_scope), include_tests
    )
    new_side = _side(
        fetch_all, graphs[new.project], PortScope(new.project, new_scope), include_tests
    )
    pairing = _pair(old_side, new_side, pairs)
    twin_of_old = {o: n for o, n, _ in pairing.pairs}
    twin_of_new = {n: o for o, n, _ in pairing.pairs}
    reports = sorted(
        (
            _pair_report(old_side, new_side, o, n, how, twin_of_old, twin_of_new)
            for o, n, how in pairing.pairs
        ),
        key=_report_order,
    )
    verdicts = [r["verdict"] for r in reports]
    how = [r["paired_by"] for r in reports]
    return ParityReport(
        old=_side_summary(old_side),
        new=_side_summary(new_side),
        summary=ParitySummary(
            pairs=len(reports),
            paired_by=PairedByCounts(
                explicit=how.count(cs.PairedBy.EXPLICIT),
                module=how.count(cs.PairedBy.MODULE),
                container=how.count(cs.PairedBy.CONTAINER),
                name=how.count(cs.PairedBy.NAME),
            ),
            match=verdicts.count(cs.ParityVerdict.MATCH),
            differs=verdicts.count(cs.ParityVerdict.DIFFERS),
            unknown=verdicts.count(cs.ParityVerdict.UNKNOWN),
            old_only=len(pairing.old_only),
            new_only=len(pairing.new_only),
            ambiguous=len(pairing.ambiguous),
        ),
        pairs=reports,
        old_only=_refs(old_side, pairing.old_only),
        new_only=_refs(new_side, pairing.new_only),
        ambiguous=[
            AmbiguousName(
                name=name, old=_refs(old_side, olds), new=_refs(new_side, news)
            )
            for name, olds, news in pairing.ambiguous
        ],
    )


# --- reading a side -------------------------------------------------------------


def _text(value: ResultValue | PropertyValue) -> str:
    return value if isinstance(value, str) else ""


def _normalise_scope(scope: str) -> str:
    path = portable_posix(scope.strip())
    return "" if path in cs.PORT_SCOPE_ROOTS else path


def _in_scope(path: str, scope: str) -> bool:
    return not scope or path == scope or path.startswith(scope + cs.SEPARATOR_SLASH)


def _overlaps(a: str, b: str) -> bool:
    return _in_scope(a, b) or _in_scope(b, a)


def _container(label: str, qualified_name: str, name: str, path: str) -> str:
    # A method's container is its class; the name is located from the right,
    # so a Java or C# qualified name ending in its parameter list still finds
    # the class before it (`Beta.low(int)`).
    if label == cs.NodeLabel.METHOD.value:
        cut = qualified_name.rfind(cs.SEPARATOR_DOT + name)
        owner = (
            qualified_name[:cut]
            if cut > 0
            else qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[0]
        )
        return normalize_name(owner.rsplit(cs.SEPARATOR_DOT, 1)[-1])
    file = PurePosixPath(path)
    stem = file.parent.name if file.stem in cs.PORT_PACKAGE_ENTRY_STEMS else file.stem
    return normalize_name(stem)


def _module_path(path: str, scope: str) -> str:
    # Relative to the scope, so `legacy/a/utils.py` and `rewrite/a/utils.rs`
    # agree; a package entry file stands for its directory, as in _container.
    if not scope:
        relative = path
    elif path.startswith(scope + cs.SEPARATOR_SLASH):
        relative = path[len(scope) + 1 :]
    else:
        # The scope is the file itself.
        relative = PurePosixPath(path).name
    file = PurePosixPath(relative)
    parts = (
        file.parent.parts
        if file.stem in cs.PORT_PACKAGE_ENTRY_STEMS
        else (*file.parent.parts, file.stem)
    )
    return cs.SEPARATOR_SLASH.join(normalize_name(part) for part in parts)


def _callable(label: str, qualified_name: str, props: PropertyDict) -> _Callable:
    name = (
        _text(props.get(cs.KEY_NAME)) or qualified_name.rsplit(cs.SEPARATOR_DOT, 1)[-1]
    )
    path = _text(props.get(cs.KEY_PATH))
    start = props.get(cs.KEY_START_LINE)
    decorators = props.get(cs.KEY_DECORATORS)
    types = props.get(cs.KEY_PARAM_TYPES)
    return _Callable(
        qualified_name=qualified_name,
        label=label,
        key=normalize_name(name),
        container=_container(label, qualified_name, name, path),
        path=path,
        start_line=start if isinstance(start, int) and start > 0 else None,
        decorators=tuple(decorators) if isinstance(decorators, list) else (),
        declared=len(types) if isinstance(types, list) else 0,
    )


def _merge_resolution(
    callees: dict[str, str | None], callee: str, resolution: str | None
) -> None:
    # Several sites may call the same callee; one exact site makes the call
    # certain, whatever the others were bound by.
    if callee not in callees or callees[callee] in cs.PORT_UNCERTAIN_RESOLUTIONS:
        callees[callee] = resolution


def _owned_nodes(
    fetch_all: QueryFn, project: str, params: PropertyDict
) -> dict[_NodeId, PropertyDict]:
    owns = _owner_check(fetch_all, project)
    nodes: dict[_NodeId, PropertyDict] = {}
    for row in fetch_all(cq.CYPHER_DEAD_CODE_NODES, params):
        qualified_name = _text(row.get(cs.KEY_QUALIFIED_NAME))
        if qualified_name and owns(qualified_name):
            nodes[(_text(row.get(cs.KEY_LABEL)), qualified_name)] = _node_props(row)
    return nodes


def _load_project(fetch_all: QueryFn, project: str) -> _ProjectGraph:
    params: PropertyDict = {cs.KEY_PROJECT_PREFIX: _prefix(project)}
    nodes = _owned_nodes(fetch_all, project, params)
    rust_modules = _rust_test_modules_from_nodes(nodes)
    rust_spans = _rust_test_fn_spans(nodes)
    callables: dict[str, _Callable] = {}
    tests: set[str] = set()
    for (label, raw_qn), props in nodes.items():
        if label not in cs.PORT_CALLABLE_LABELS:
            continue
        qualified_name = str(raw_qn)
        found = _callable(label, qualified_name, props)
        callables[qualified_name] = found
        if _is_test_symbol(props, qualified_name, found.path, rust_modules, rust_spans):
            tests.add(qualified_name)
    return _ProjectGraph(
        callables, frozenset(tests), _project_calls(fetch_all, params, callables)
    )


def _project_calls(
    fetch_all: QueryFn, params: PropertyDict, callables: dict[str, _Callable]
) -> dict[str, dict[str, str | None]]:
    calls: dict[str, dict[str, str | None]] = {}
    for row in fetch_all(cq.CYPHER_DEAD_CODE_RELS, params):
        if _text(row.get(cs.KEY_REL_TYPE)) != cs.RelationshipType.CALLS.value:
            continue
        caller = _text(row.get(cs.KEY_FROM_QN))
        callee = _text(row.get(cs.KEY_TO_QN))
        if caller in callables and callee in callables:
            resolution = row.get(cs.KEY_RESOLUTION)
            _merge_resolution(
                calls.setdefault(caller, {}),
                callee,
                resolution if isinstance(resolution, str) else None,
            )
    return calls


def _positional_params(
    fetch_all: QueryFn, project: str, paths: Iterable[str]
) -> dict[str, tuple[str, ...]]:
    params: PropertyDict = {
        cs.KEY_PROJECT_PREFIX: _prefix(project),
        cs.KEY_LONGER_PROJECT_PREFIXES: list(
            _longer_project_prefixes(fetch_all, project)
        ),
        cs.CYPHER_PARAM_PATHS: sorted(set(paths)),
    }
    out: dict[str, tuple[str, ...]] = {}
    for row in fetch_all(cq.CYPHER_DELTA_DEFINITIONS, params):
        qualified_name = _text(row.get(cs.KEY_QUALIFIED_NAME))
        value = row.get(cs.KEY_POSITIONAL_PARAMS)
        # Absent means the language records no positional parameters, which
        # is not the same as declaring none: it stays absent here.
        if qualified_name and isinstance(value, list):
            out[qualified_name] = tuple(str(v) for v in value)
    return out


def _side(
    fetch_all: QueryFn, graph: _ProjectGraph, spec: PortScope, include_tests: bool
) -> _Side:
    chosen = {
        qn: found._replace(module=_module_path(found.path, spec.scope))
        for qn, found in graph.callables.items()
        if _in_scope(found.path, spec.scope)
        and (include_tests or qn not in graph.tests)
    }
    if not chosen:
        raise PortCheckError(
            cs.PORT_ERR_EMPTY_SCOPE.format(
                project=spec.project, scope=spec.scope or cs.SEPARATOR_DOT
            )
        )
    calls = {
        caller: {callee: res for callee, res in callees.items() if callee in chosen}
        for caller, callees in graph.calls.items()
        if caller in chosen
    }
    positional = _positional_params(
        fetch_all, spec.project, (found.path for found in chosen.values())
    )
    return _Side(spec, chosen, calls, positional)


# --- pairing --------------------------------------------------------------------


def _resolve_target(side: _Side, target: str) -> str:
    if target in side.callables:
        return target
    matches = [qn for qn in side.callables if qn.endswith(cs.SEPARATOR_DOT + target)]
    if len(matches) != 1:
        raise PortCheckError(
            cs.PORT_ERR_PAIR_TARGET.format(
                target=target,
                count=len(matches),
                scope=side.spec.scope or cs.SEPARATOR_DOT,
            )
        )
    return matches[0]


def _tier_key(found: _Callable, tier: cs.PairedBy) -> tuple[str, str, str]:
    # Keys are only compared within one tier, so a field the tier ignores is
    # left blank rather than dropped.
    match tier:
        case cs.PairedBy.MODULE:
            return (found.key, found.module, found.container)
        case cs.PairedBy.CONTAINER:
            return (found.key, "", found.container)
        case _:
            return (found.key, "", "")


def _group(
    callables: Iterable[_Callable], tier: cs.PairedBy
) -> dict[tuple[str, str, str], list[_Callable]]:
    groups: dict[tuple[str, str, str], list[_Callable]] = {}
    for found in callables:
        groups.setdefault(_tier_key(found, tier), []).append(found)
    return groups


def _pair_unique(
    olds: Iterable[_Callable],
    news: Iterable[_Callable],
    tier: cs.PairedBy,
    pairing: _Pairing,
) -> tuple[list[_Callable], list[_Callable]]:
    """Pair every group holding one callable on each side; return the rest."""
    old_groups = _group(olds, tier)
    new_groups = _group(news, tier)
    left_old: list[_Callable] = []
    left_new: list[_Callable] = []
    for key in sorted(old_groups.keys() | new_groups.keys()):
        in_old = old_groups.get(key, [])
        in_new = new_groups.get(key, [])
        if len(in_old) == 1 and len(in_new) == 1:
            pairing.pairs.append(
                (in_old[0].qualified_name, in_new[0].qualified_name, tier)
            )
        else:
            left_old.extend(in_old)
            left_new.extend(in_new)
    return left_old, left_new


def _pair_explicit(
    old: _Side, new: _Side, explicit: Sequence[tuple[str, str]], pairing: _Pairing
) -> tuple[set[str], set[str]]:
    """Pair what `--pair` names; return the qualified names it took per side."""
    taken_old: set[str] = set()
    taken_new: set[str] = set()
    for old_target, new_target in explicit:
        o = _resolve_target(old, old_target)
        n = _resolve_target(new, new_target)
        if o in taken_old or n in taken_new:
            raise PortCheckError(
                cs.PORT_ERR_PAIR_TWICE.format(
                    qualified_name=o if o in taken_old else n
                ),
                cs.PORT_EXIT_USAGE,
            )
        taken_old.add(o)
        taken_new.add(n)
        pairing.pairs.append((o, n, cs.PairedBy.EXPLICIT))
    return taken_old, taken_new


def _pair(old: _Side, new: _Side, explicit: Sequence[tuple[str, str]]) -> _Pairing:
    pairing = _Pairing([], [], [], [])
    taken_old, taken_new = _pair_explicit(old, new, explicit, pairing)
    # Strongest evidence first, each tier on what the previous one left: the
    # same module path, then the same container when the two layouts differ
    # (`lexer.py` against `cypher/lexer.rs`), then the name alone, where
    # `paired_by: name` tells the reader the containers differ.
    left_old = [c for qn, c in old.callables.items() if qn not in taken_old]
    left_new = [c for qn, c in new.callables.items() if qn not in taken_new]
    for tier in cs.PORT_PAIRING_TIERS:
        left_old, left_new = _pair_unique(left_old, left_new, tier, pairing)
    _settle_leftovers(left_old, left_new, pairing)
    return pairing


def _settle_leftovers(
    left_old: list[_Callable], left_new: list[_Callable], pairing: _Pairing
) -> None:
    """What no tier paired: ambiguous where both sides still share a name,
    otherwise unpaired on its own side."""
    old_by_name = _group(left_old, cs.PairedBy.NAME)
    new_by_name = _group(left_new, cs.PairedBy.NAME)
    for key in sorted(old_by_name.keys() | new_by_name.keys()):
        olds = sorted(c.qualified_name for c in old_by_name.get(key, []))
        news = sorted(c.qualified_name for c in new_by_name.get(key, []))
        if olds and news:
            pairing.ambiguous.append((key[0], olds, news))
        else:
            pairing.old_only.extend(olds)
            pairing.new_only.extend(news)
    pairing.old_only.sort()
    pairing.new_only.sort()


# --- judging a pair -------------------------------------------------------------


def _calls_parity(
    old_callees: dict[str, str | None],
    new_callees: dict[str, str | None],
    twin_of_old: dict[str, str],
    twin_of_new: dict[str, str],
) -> CallsParity:
    expected = {
        twin_of_old[callee]: (callee, res)
        for callee, res in old_callees.items()
        if callee in twin_of_old
    }
    missing = [
        CallDifference(old=callee, new=twin, resolution=res)
        for twin, (callee, res) in sorted(expected.items())
        if twin not in new_callees
    ]
    extra = [
        CallDifference(old=twin_of_new[callee], new=callee, resolution=res)
        for callee, res in sorted(new_callees.items())
        if callee in twin_of_new and callee not in expected
    ]
    differences = missing + extra
    if not differences:
        verdict = cs.ParityVerdict.MATCH
    elif all(d["resolution"] in cs.PORT_UNCERTAIN_RESOLUTIONS for d in differences):
        verdict = cs.ParityVerdict.UNKNOWN
    else:
        verdict = cs.ParityVerdict.DIFFERS
    return CallsParity(
        verdict=verdict,
        missing=missing,
        extra=extra,
        unpaired_old=sorted(c for c in old_callees if c not in twin_of_old),
        unpaired_new=sorted(c for c in new_callees if c not in twin_of_new),
    )


def _arity(found: _Callable, positional: tuple[str, ...] | None) -> _Arity | None:
    if positional is None:
        return None
    language = _language(found.path)
    if language == cs.SupportedLanguage.PYTHON:
        # Python records CPython's positional names, receiver included, and
        # stops at `*args` or `*`; `param_types` lists every declared
        # parameter, so more of those than positional names means a `*args`,
        # keyword-only or `**kwargs` parameter the count cannot speak for.
        names = list(positional)
        if (
            found.label == cs.NodeLabel.METHOD.value
            and names
            and not _is_static_decorator(list(found.decorators))
        ):
            names = names[1:]
        return _Arity(len(names), found.declared > len(positional))
    # Only the language that writes a receiver marker loses it: a JavaScript
    # `function f(self)` takes one argument, as its Python twin does.
    kept = [p for p in positional if not _is_receiver(p, language)]
    rest = [p for p in kept if p.startswith(cs.POSITIONAL_REST_PREFIX)]
    return _Arity(len(kept) - len(rest), bool(rest))


def _arity_parity(old: _Arity | None, new: _Arity | None) -> ArityParity:
    if old is None or new is None or old.open or new.open:
        verdict = cs.ParityVerdict.UNKNOWN
    elif old.fixed == new.fixed:
        verdict = cs.ParityVerdict.MATCH
    else:
        verdict = cs.ParityVerdict.DIFFERS
    return ArityParity(
        verdict=verdict,
        old=None if old is None else old.fixed,
        new=None if new is None else new.fixed,
        open=bool(old and old.open) or bool(new and new.open),
    )


def _worst(*verdicts: cs.ParityVerdict) -> cs.ParityVerdict:
    return max(verdicts, key=cs.PARITY_VERDICT_RANK.__getitem__)


def _ref(side: _Side, qualified_name: str) -> SymbolRef:
    found = side.callables[qualified_name]
    return SymbolRef(
        qualified_name=qualified_name, path=found.path, start_line=found.start_line
    )


def _refs(side: _Side, qualified_names: Iterable[str]) -> list[SymbolRef]:
    return [_ref(side, qn) for qn in qualified_names]


def _pair_report(
    old: _Side,
    new: _Side,
    o: str,
    n: str,
    paired_by: cs.PairedBy,
    twin_of_old: dict[str, str],
    twin_of_new: dict[str, str],
) -> PairReport:
    calls = _calls_parity(
        old.calls.get(o, {}), new.calls.get(n, {}), twin_of_old, twin_of_new
    )
    arity = _arity_parity(
        _arity(old.callables[o], old.positional.get(o)),
        _arity(new.callables[n], new.positional.get(n)),
    )
    return PairReport(
        old=_ref(old, o),
        new=_ref(new, n),
        paired_by=paired_by,
        verdict=_worst(calls["verdict"], arity["verdict"]),
        calls=calls,
        arity=arity,
    )


def _report_order(report: PairReport) -> tuple[str, int, str]:
    old = report["old"]
    return (old["path"], old["start_line"] or 0, old["qualified_name"])


def _side_summary(side: _Side) -> SideSummary:
    return SideSummary(
        project=side.spec.project,
        scope=side.spec.scope or cs.SEPARATOR_DOT,
        definitions=len(side.callables),
    )
