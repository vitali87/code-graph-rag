"""Put cgr's CALLS edges in the oracle's terms: definition sites.

A SCIP definition is (file, line of the defining name, name). A cgr node has
a file, a span and a name. They are the same definition when the cgr node is
the innermost one in that file, with that name, whose span covers the line,
so neither side's naming scheme has to be translated into the other's.

What does not join keeps its own identity and is scored as it is: a cgr edge
to a node SCIP does not consider a callable is a false positive, a SCIP edge
into a callable cgr has no node for is a miss. The exception is cgr's
anonymous functions (`anonymous_<line>_<col>`, `iife_arrow_...`): SCIP gives an anonymous function no
symbol and credits the calls inside it to the enclosing named callable, so
cgr's are folded the same way and calls *to* them are not scored.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import NamedTuple

from codebase_rag import constants as cs
from codebase_rag.types_defs import PropertyValue

from .. import constants as ec
from ..cgr_graph import _CapturingIngestor
from . import constants as sc
from .calls import module_definition
from .types_defs import Definition

_CALLABLE_LABELS = frozenset({cs.NodeLabel.FUNCTION.value, cs.NodeLabel.METHOD.value})
_MODULE = cs.NodeLabel.MODULE.value
_CGR_ANONYMOUS = re.compile(ec.CGR_ANONYMOUS_PATTERN)


class CgrEdges(NamedTuple):
    edges: set[tuple[Definition, Definition]]
    exact: set[tuple[Definition, Definition]]
    # SCIP callables a cgr node joined to, and cgr callables none joined to.
    joined: frozenset[Definition]
    unmatched: list[Definition]


class _Node(NamedTuple):
    qn: str
    path: str
    name: str
    start: int
    end: int


def _int(value: PropertyValue | None) -> int | None:
    return int(value) if isinstance(value, int | float) else None


def _nodes(
    ingestor: _CapturingIngestor, suffixes: tuple[str, ...]
) -> tuple[list[_Node], dict[str, str]]:
    callables: list[_Node] = []
    modules: dict[str, str] = {}
    for (label, uid), props in ingestor.nodes.items():
        path = props.get(cs.KEY_PATH)
        if path is None or not str(path).endswith(suffixes):
            continue
        if label == _MODULE:
            modules[str(uid)] = str(path)
            continue
        if label not in _CALLABLE_LABELS:
            continue
        start, end = (
            _int(props.get(cs.KEY_START_LINE)),
            _int(props.get(cs.KEY_END_LINE)),
        )
        if start is None:
            continue
        callables.append(
            _Node(
                qn=str(uid),
                path=str(path),
                name=str(props.get(cs.KEY_NAME, "")),
                start=start,
                end=end if end is not None else start,
            )
        )
    return callables, modules


def _join(
    nodes: list[_Node], definitions: frozenset[Definition]
) -> dict[str, Definition]:
    by_key: dict[tuple[str, str], list[_Node]] = defaultdict(list)
    for node in nodes:
        by_key[(node.path, node.name)].append(node)
    joined: dict[str, Definition] = {}
    for definition in sorted(definitions):
        # cgr lines are one-based, SCIP lines zero-based.
        line = definition.line + 1
        covering = [
            n
            for n in by_key.get((definition.path, definition.name), ())
            if n.start <= line <= n.end and n.qn not in joined
        ]
        if covering:
            innermost = min(covering, key=lambda n: (n.end - n.start, -n.start))
            joined[innermost.qn] = definition
    return joined


def _owner(qn: str, known: dict[str, Definition]) -> Definition | None:
    """The named definition an anonymous cgr function's calls belong to."""
    segments = qn.split(cs.SEPARATOR_DOT)
    for index, segment in enumerate(segments):
        if _CGR_ANONYMOUS.match(segment):
            return known.get(cs.SEPARATOR_DOT.join(segments[:index]))
    return known.get(qn)


def cgr_edges(
    ingestor: _CapturingIngestor,
    calls: list[tuple[str, str, str, str, str]],
    suffixes: tuple[str, ...],
    definitions: frozenset[Definition],
    covered: frozenset[str],
) -> CgrEdges:
    """cgr's edges between files the index covers, as definition sites.

    Grading stops at the oracle's coverage: a file the indexer skipped (or
    could not parse) has no truth to hold cgr's edges against.
    """
    nodes, modules = _nodes(ingestor, suffixes)
    nodes = [n for n in nodes if n.path in covered]
    modules = {qn: path for qn, path in modules.items() if path in covered}
    joined = _join(nodes, definitions)
    known: dict[str, Definition] = {
        qn: module_definition(path) for qn, path in modules.items()
    }
    unmatched: list[Definition] = []
    for node in nodes:
        if node.qn in joined:
            known[node.qn] = joined[node.qn]
        elif not any(_CGR_ANONYMOUS.match(s) for s in node.qn.split(cs.SEPARATOR_DOT)):
            # Keyed by its own start line so it can never collide with a
            # SCIP definition it failed to join.
            own = Definition(node.path, node.start - 1, node.name)
            known[node.qn] = own
            unmatched.append(own)
    edges: set[tuple[Definition, Definition]] = set()
    exact: set[tuple[Definition, Definition]] = set()
    for from_label, from_qn, to_label, to_qn, resolution in calls:
        if to_label not in _CALLABLE_LABELS or from_label not in (
            _CALLABLE_LABELS | {_MODULE}
        ):
            continue
        callee = known.get(to_qn)
        caller = _owner(from_qn, known)
        if callee is None or caller is None:
            continue
        edges.add((caller, callee))
        if resolution == cs.EdgeResolution.EXACT.value:
            exact.add((caller, callee))
    return CgrEdges(
        edges=edges,
        exact=exact,
        joined=frozenset(joined.values()),
        unmatched=sorted(unmatched),
    )


def definition_repr(definition: Definition) -> str:
    if definition.line == sc.MODULE_LINE:
        return sc.MODULE_REPR.format(path=definition.path)
    return sc.DEFINITION_REPR.format(
        path=definition.path, line=definition.line + 1, name=definition.name
    )
