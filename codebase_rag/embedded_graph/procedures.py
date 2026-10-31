"""Graph algorithms callable from Cypher (`CALL pagerank.get() YIELD ...`).

Named after the MAGE procedures cgr's Memgraph prompt already teaches, so a
model's queries carry over. Each loads the adjacency it needs from SQLite and
runs in Python; when the optional Rust extension (`cgr_graph_rs`, see
rust/cgr_graph_rs and native.py) is importable, the heavy ones run there.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..constants.embedded_graph import (
    EG_DEGREE_UNDIRECTED,
    EG_PROC_ANCESTORS,
    EG_PROC_BETWEENNESS,
    EG_PROC_CYCLES,
    EG_PROC_DEGREE,
    EG_PROC_DESCENDANTS,
    EG_PROC_FIND_CYCLE,
    EG_PROC_PAGERANK,
    EG_PROC_SCC,
    EG_PROC_SHORTEST_PATH,
    EG_PROC_TOPOSORT,
    EG_PROC_WCC,
    EgDirection,
    EgProcCol,
)
from . import native
from . import sql as sq
from . import types as ty
from .limits import check_deadline
from .types import SqlValue, T

# A record field: a node (its id), a number, or nodes in a list or lists.
type ProcValue = int | float | list[int] | list[list[int]]
Record = dict[str, ProcValue]

_DEFAULT_ITERATIONS = 100
_DEFAULT_DAMPING = 0.85
_DEFAULT_EPSILON = 1e-5
# A cycle listing is exponential in the worst case; past this many the
# answer is truncated rather than hanging the query.
_MAX_CYCLES = 1000


@dataclass(frozen=True)
class ProcSpec:
    columns: tuple[tuple[str, T], ...]
    fn: Callable[[sqlite3.Connection, Sequence[SqlValue]], Iterable[Record]]


def _graph(
    conn: sqlite3.Connection,
) -> tuple[list[int], dict[int, list[int]], list[tuple[int, int, int]]]:
    nodes = [int(r[0]) for r in conn.execute(sq.NODE_IDS_IN_ORDER)]
    edges = [(int(e), int(s), int(d)) for e, s, d in conn.execute(sq.EDGES_IN_ORDER)]
    out: dict[int, list[int]] = defaultdict(list)
    for _, s, d in edges:
        out[s].append(d)
    return nodes, out, edges


def _arg(args: Sequence[SqlValue], i: int, default: SqlValue) -> SqlValue:
    return args[i] if len(args) > i and args[i] is not None else default


def _number_arg(args: Sequence[SqlValue], i: int, default: float) -> float:
    value = _arg(args, i, default)
    return float(value) if isinstance(value, int | float) else default


def _node_arg(args: Sequence[SqlValue], i: int) -> int | None:
    """A node argument: the engine passes nodes as their integer ids."""
    value = _arg(args, i, None)
    return value if isinstance(value, int) else None


def _spread_rank(
    nodes: list[int], out: dict[int, list[int]], rank: dict[int, float]
) -> dict[int, float]:
    """The rank each node receives along its in-edges in one iteration."""
    nxt = dict.fromkeys(nodes, 0.0)
    for v in nodes:
        targets = out.get(v)
        if targets:
            share = rank[v] / len(targets)
            for w in targets:
                nxt[w] += share
    return nxt


def _pagerank_step(
    nodes: list[int], out: dict[int, list[int]], rank: dict[int, float], damping: float
) -> float:
    """Advance `rank` one iteration in place; return how far it moved."""
    n = len(nodes)
    # A plain running sum, not sum(): Python's compensates rounding,
    # and the Rust path must produce the same ranks bit for bit.
    dangling = 0.0
    for v in nodes:
        if not out.get(v):
            dangling += rank[v]
    nxt = _spread_rank(nodes, out, rank)
    base = (1.0 - damping) / n + damping * dangling / n
    delta = 0.0
    for v in nodes:
        value = base + damping * nxt[v]
        delta += abs(value - rank[v])
        rank[v] = value
    return delta


def _pagerank(conn: sqlite3.Connection, args: Sequence[SqlValue]) -> Iterable[Record]:
    iterations = int(_number_arg(args, 0, _DEFAULT_ITERATIONS))
    damping = _number_arg(args, 1, _DEFAULT_DAMPING)
    epsilon = _number_arg(args, 2, _DEFAULT_EPSILON)
    nodes, out, _ = _graph(conn)
    n = len(nodes)
    if not n:
        return []
    rank = dict.fromkeys(nodes, 1.0 / n)
    for _ in range(iterations):
        check_deadline()
        if _pagerank_step(nodes, out, rank, damping) < epsilon:
            break
    return [{EgProcCol.NODE: v, EgProcCol.RANK: rank[v]} for v in nodes]


def _degree(conn: sqlite3.Connection, args: Sequence[SqlValue]) -> Iterable[Record]:
    mode = str(_arg(args, 0, EG_DEGREE_UNDIRECTED)).lower()
    counts: dict[int, int] = defaultdict(int)
    for s, d in conn.execute(sq.EDGE_ENDS):
        if mode in (EgDirection.OUT, EG_DEGREE_UNDIRECTED):
            counts[int(s)] += 1
        if mode in (EgDirection.IN, EG_DEGREE_UNDIRECTED):
            counts[int(d)] += 1
    return [
        {EgProcCol.NODE: int(r[0]), EgProcCol.DEGREE: counts.get(int(r[0]), 0)}
        for r in conn.execute(sq.NODE_IDS_IN_ORDER)
    ]


def _wcc(conn: sqlite3.Connection, _args: Sequence[SqlValue]) -> Iterable[Record]:
    nodes, _, edges = _graph(conn)
    parent = {v: v for v in nodes}

    def find(v: int) -> int:
        while parent[v] != v:
            parent[v] = parent[parent[v]]
            v = parent[v]
        return v

    for _, s, d in edges:
        rs, rd = find(s), find(d)
        if rs != rd:
            parent[max(rs, rd)] = min(rs, rd)
    ids: dict[int, int] = {}
    return [
        {EgProcCol.NODE: v, EgProcCol.COMPONENT_ID: ids.setdefault(find(v), len(ids))}
        for v in nodes
    ]


class _TarjanState:
    """The bookkeeping Tarjan's algorithm carries across its work stack."""

    def __init__(self) -> None:
        self.index: dict[int, int] = {}
        self.low: dict[int, int] = {}
        self.on_stack: set[int] = set()
        self.stack: list[int] = []
        self.comps: list[list[int]] = []
        self.counter = 0

    def enter(self, v: int) -> None:
        """Number `v` and push it onto the current path."""
        self.index[v] = self.low[v] = self.counter
        self.counter += 1
        self.stack.append(v)
        self.on_stack.add(v)

    def pop_component(self, v: int) -> None:
        """Unwind the path down to `v`, which closes one component."""
        comp = []
        while True:
            w = self.stack.pop()
            self.on_stack.discard(w)
            comp.append(w)
            if w == v:
                break
        self.comps.append(comp)


def _tarjan_step(
    state: _TarjanState, out: dict[int, list[int]], work: list[tuple[int, int]]
) -> None:
    """Take one frame off the work stack: follow its next edge, or finish it."""
    v, i = work.pop()
    if i == 0:
        state.enter(v)
    targets = out.get(v, [])
    if i < len(targets):
        work.append((v, i + 1))
        w = targets[i]
        if w not in state.index:
            work.append((w, 0))
        elif w in state.on_stack:
            state.low[v] = min(state.low[v], state.index[w])
        return
    if state.low[v] == state.index[v]:
        state.pop_component(v)
    if work:
        parent = work[-1][0]
        state.low[parent] = min(state.low[parent], state.low[v])


def _tarjan(nodes: list[int], out: dict[int, list[int]]) -> list[list[int]]:
    state = _TarjanState()
    for root in nodes:
        if root in state.index:
            continue
        check_deadline()
        work = [(root, 0)]
        while work:
            _tarjan_step(state, out, work)
    return state.comps


def _scc(conn: sqlite3.Connection, _args: Sequence[SqlValue]) -> Iterable[Record]:
    nodes, out, _ = _graph(conn)
    return [{EgProcCol.COMPONENTS: _tarjan(nodes, out)}]


def _cycles_through(
    start: int, members: set[int], out: dict[int, list[int]], cycles: list[list[int]]
) -> None:
    """Append the cycles through `start` that stay within `members`."""
    stack = [(start, [start])]
    while stack and len(cycles) < _MAX_CYCLES:
        check_deadline()
        v, path = stack.pop()
        for w in out.get(v, []):
            if w == start:
                cycles.append(path)
            elif w in members and w > start and w not in path:
                stack.append((w, [*path, w]))


def _simple_cycles(
    conn: sqlite3.Connection, _args: Sequence[SqlValue]
) -> Iterable[Record]:
    nodes, out, _ = _graph(conn)
    cycles: list[list[int]] = []
    for comp in _tarjan(nodes, out):
        members = set(comp)
        if len(comp) == 1 and comp[0] not in out.get(comp[0], []):
            continue
        for start in sorted(comp):
            # Cycles through `start` using only members >= start: each cycle
            # is reported once, from its smallest node.
            _cycles_through(start, members, out, cycles)
            members.discard(start)
            if len(cycles) >= _MAX_CYCLES:
                return [{EgProcCol.CYCLES: cycles}]
    return [{EgProcCol.CYCLES: cycles}]


def _find_cycle(conn: sqlite3.Connection, args: Sequence[SqlValue]) -> Iterable[Record]:
    for record in _simple_cycles(conn, args):
        cycles = record[EgProcCol.CYCLES]
        assert isinstance(cycles, list)
        return [{EgProcCol.CYCLE: cycles[0] if cycles else []}]
    return [{EgProcCol.CYCLE: []}]


def _toposort(conn: sqlite3.Connection, _args: Sequence[SqlValue]) -> Iterable[Record]:
    nodes, out, _ = _graph(conn)
    indegree = dict.fromkeys(nodes, 0)
    for v in nodes:
        for w in out.get(v, []):
            indegree[w] += 1
    queue = deque(v for v in nodes if indegree[v] == 0)
    order: list[int] = []
    while queue:
        check_deadline()
        v = queue.popleft()
        order.append(v)
        for w in out.get(v, []):
            indegree[w] -= 1
            if indegree[w] == 0:
                queue.append(w)
    return [{EgProcCol.NODES: order if len(order) == len(nodes) else []}]


def _shortest_path(
    conn: sqlite3.Connection, args: Sequence[SqlValue]
) -> Iterable[Record]:
    source, target = _node_arg(args, 0), _node_arg(args, 1)
    if source is None or target is None:
        return [{EgProcCol.PATH: []}]
    _, out, _ = _graph(conn)
    prev: dict[int, int | None] = {source: None}
    queue = deque([source])
    while queue:
        check_deadline()
        v = queue.popleft()
        if v == target:
            break
        for w in out.get(v, []):
            if w not in prev:
                prev[w] = v
                queue.append(w)
    if target not in prev:
        return [{EgProcCol.PATH: []}]
    path: list[int] = []
    step: int | None = target
    while step is not None:
        path.append(step)
        step = prev[step]
    return [{EgProcCol.PATH: path[::-1]}]


def _shortest_path_counts(
    s: int, nodes: list[int], out: dict[int, list[int]]
) -> tuple[list[int], dict[int, list[int]], dict[int, float]]:
    """Brandes' forward pass from `s`: the nodes in the order reached, each
    one's predecessors on a shortest path, and its count of shortest paths."""
    stack: list[int] = []
    preds: dict[int, list[int]] = defaultdict(list)
    sigma = dict.fromkeys(nodes, 0.0)
    sigma[s] = 1.0
    dist = dict.fromkeys(nodes, -1)
    dist[s] = 0
    queue = deque([s])
    while queue:
        v = queue.popleft()
        stack.append(v)
        for w in out.get(v, []):
            if dist[w] < 0:
                dist[w] = dist[v] + 1
                queue.append(w)
            if dist[w] == dist[v] + 1:
                sigma[w] += sigma[v]
                preds[w].append(v)
    return stack, preds, sigma


def _add_dependencies(
    s: int, nodes: list[int], out: dict[int, list[int]], score: dict[int, float]
) -> None:
    """Brandes' backward pass: add `s`'s dependency on each node to its score."""
    stack, preds, sigma = _shortest_path_counts(s, nodes, out)
    delta = dict.fromkeys(nodes, 0.0)
    while stack:
        w = stack.pop()
        for v in preds[w]:
            delta[v] += sigma[v] / sigma[w] * (1 + delta[w])
        if w != s:
            score[w] += delta[w]


def _betweenness(
    conn: sqlite3.Connection, _args: Sequence[SqlValue]
) -> Iterable[Record]:
    nodes, out, _ = _graph(conn)
    score = dict.fromkeys(nodes, 0.0)
    for s in nodes:
        check_deadline()
        _add_dependencies(s, nodes, out, score)
    n = len(nodes)
    scale = 1.0 / ((n - 1) * (n - 2)) if n > 2 else 1.0
    return [{EgProcCol.NODE: v, EgProcCol.BETWEENNESS: score[v] * scale} for v in nodes]


def _reach(
    conn: sqlite3.Connection, node: int | None, column: str, sql: str
) -> Iterable[Record]:
    if node is None:
        return [{column: []}]
    seen: set[int] = set()
    queue = deque([node])
    while queue:
        check_deadline()
        v = queue.popleft()
        for (w,) in conn.execute(sql, (v,)):
            if w not in seen and w != node:
                seen.add(int(w))
                queue.append(int(w))
    return [{column: sorted(seen)}]


def _ancestors(conn: sqlite3.Connection, args: Sequence[SqlValue]) -> Iterable[Record]:
    return _reach(
        conn,
        _node_arg(args, 0),
        EgProcCol.ANCESTORS,
        sq.SOURCES_OF,
    )


def _descendants(
    conn: sqlite3.Connection, args: Sequence[SqlValue]
) -> Iterable[Record]:
    return _reach(
        conn,
        _node_arg(args, 0),
        EgProcCol.DESCENDANTS,
        sq.TARGETS_OF,
    )


_NODES = ty.list_of(ty.NODE)

PROCEDURES: dict[str, ProcSpec] = {
    EG_PROC_PAGERANK: ProcSpec(
        ((EgProcCol.NODE, ty.NODE), (EgProcCol.RANK, ty.FLOAT)), _pagerank
    ),
    EG_PROC_DEGREE: ProcSpec(
        ((EgProcCol.NODE, ty.NODE), (EgProcCol.DEGREE, ty.INT)), _degree
    ),
    EG_PROC_WCC: ProcSpec(
        ((EgProcCol.NODE, ty.NODE), (EgProcCol.COMPONENT_ID, ty.INT)), _wcc
    ),
    EG_PROC_SCC: ProcSpec(((EgProcCol.COMPONENTS, ty.list_of(_NODES)),), _scc),
    EG_PROC_CYCLES: ProcSpec(((EgProcCol.CYCLES, ty.list_of(_NODES)),), _simple_cycles),
    EG_PROC_FIND_CYCLE: ProcSpec(((EgProcCol.CYCLE, _NODES),), _find_cycle),
    EG_PROC_TOPOSORT: ProcSpec(((EgProcCol.NODES, _NODES),), _toposort),
    EG_PROC_SHORTEST_PATH: ProcSpec(((EgProcCol.PATH, _NODES),), _shortest_path),
    EG_PROC_BETWEENNESS: ProcSpec(
        ((EgProcCol.NODE, ty.NODE), (EgProcCol.BETWEENNESS, ty.FLOAT)), _betweenness
    ),
    EG_PROC_ANCESTORS: ProcSpec(((EgProcCol.ANCESTORS, _NODES),), _ancestors),
    EG_PROC_DESCENDANTS: ProcSpec(((EgProcCol.DESCENDANTS, _NODES),), _descendants),
}


def run_procedure(
    conn: sqlite3.Connection,
    name: str,
    args: Sequence[SqlValue],
    external_reads_ok: bool = True,
) -> Iterable[Record]:
    """A procedure's records, from the Rust extension when it can serve them.

    The extension reads the file on its own connection, which cannot see
    writes this statement made before its CALL, so a statement that writes
    keeps to the Python implementation (`external_reads_ok=False`). It reads
    at this statement's snapshot or not at all (native.py), and declines a
    procedure it does not implement and an in-memory graph it cannot open.
    """
    if external_reads_ok:
        records = native.run_procedure(conn, name, args)
        if records is not None:
            return records
    return PROCEDURES[name].fn(conn, args)
