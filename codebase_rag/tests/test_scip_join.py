"""cgr's CALLS land on SCIP definition sites by file, name and span.

The capture is filled by hand so each joining rule has a case that breaks
it: the innermost same-named node wins, a node SCIP does not know keeps its
own identity (and so scores), an anonymous function's calls fold into its
named owner, and nothing outside the index's coverage is graded.
"""

from __future__ import annotations

from codebase_rag import constants as cs
from evals.cgr_graph import CallCapturingIngestor
from evals.scip.calls import module_definition
from evals.scip.join import cgr_edges
from evals.scip.types_defs import Definition

_FUNCTION = cs.NodeLabel.FUNCTION.value
_METHOD = cs.NodeLabel.METHOD.value
_MODULE = cs.NodeLabel.MODULE.value
_EXACT = cs.EdgeResolution.EXACT.value
_HEURISTIC = cs.EdgeResolution.HEURISTIC.value


def _node(
    ingestor: CallCapturingIngestor,
    label: str,
    qn: str,
    path: str,
    name: str = "",
    start: int | None = None,
    end: int | None = None,
) -> None:
    props: dict[str, str | int] = {cs.KEY_QUALIFIED_NAME: qn, cs.KEY_PATH: path}
    if name:
        props[cs.KEY_NAME] = name
    if start is not None:
        props[cs.KEY_START_LINE] = start
    if end is not None:
        props[cs.KEY_END_LINE] = end
    ingestor.ensure_node_batch(label, props)


def _capture() -> CallCapturingIngestor:
    ingestor = CallCapturingIngestor()
    _node(ingestor, _MODULE, "p.m", "m.py")
    _node(ingestor, _FUNCTION, "p.m.outer", "m.py", "outer", 1, 10)
    # A nested function with the outer one's name: the join must pick it.
    _node(ingestor, _FUNCTION, "p.m.outer.outer", "m.py", "outer", 3, 5)
    _node(ingestor, _FUNCTION, "p.m.helper", "m.py", "helper", 12, 13)
    _node(ingestor, _FUNCTION, "p.m.outer.anonymous_7_4", "m.py", "anonymous_7_4", 7, 8)
    _node(ingestor, _METHOD, "p.m.C.ghost", "m.py", "ghost", 20, 21)
    _node(ingestor, _FUNCTION, "p.other.far", "other.py", "far", 1, 2)
    calls = [
        (_MODULE, "p.m", _FUNCTION, "p.m.outer", _EXACT),
        (_FUNCTION, "p.m.outer.outer", _FUNCTION, "p.m.helper", _EXACT),
        # Made inside an anonymous callback: credited to `outer`, and the
        # edge into the callback itself is not graded.
        (_FUNCTION, "p.m.outer.anonymous_7_4", _FUNCTION, "p.m.helper", _HEURISTIC),
        (_FUNCTION, "p.m.outer", _FUNCTION, "p.m.outer.anonymous_7_4", _EXACT),
        (_FUNCTION, "p.m.helper", _METHOD, "p.m.C.ghost", _HEURISTIC),
        (_FUNCTION, "p.m.helper", _FUNCTION, "p.other.far", _EXACT),
    ]
    ingestor.calls.extend(calls)
    return ingestor


def test_joins_by_file_name_and_innermost_span() -> None:
    ingestor = _capture()
    outer, inner = Definition("m.py", 0, "outer"), Definition("m.py", 3, "outer")
    helper = Definition("m.py", 11, "helper")

    result = cgr_edges(
        ingestor,
        ingestor.calls,
        (".py",),
        frozenset({outer, inner, helper}),
        covered=frozenset({"m.py"}),
    )

    ghost = Definition("m.py", 19, "ghost")
    assert result.edges == {
        (module_definition("m.py"), outer),
        (inner, helper),
        (outer, helper),
        (helper, ghost),
    }
    assert result.exact == {(module_definition("m.py"), outer), (inner, helper)}
    assert result.joined == frozenset({outer, inner, helper})
    # `ghost` has no SCIP counterpart: it stays, scored as its own node.
    assert result.unmatched == [ghost]
