"""A trace confirmation finds its static edge by index, not by scanning (#3187).

Each `confirmed static` record upgrades its pair's CALLS edge with one write
whose two endpoints carried no label. Memgraph's indexes are label +
property, so every confirmation scanned every node of the whole shared
graph: 250-286 ms each at 808k nodes, 144 s to ingest an eslint trace that
takes 3 s with the endpoints labelled.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from codebase_rag import constants as cs
from codebase_rag.tests.test_edge_resolution import PROJECT, _callable, _Graph, _trace
from codebase_rag.trace.ingest import TraceGraphProtocol, ingest_trace

RUN = f"{PROJECT}.pkg.app.run"
KNOWN = f"{PROJECT}.pkg.svc.Svc.known"


def _confirmations(tmp_path: Path, callee_label: str) -> list[tuple[str, dict]]:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "app.py").write_text("def run(s):\n    s.known()\n")
    (repo / "pkg" / "svc.py").write_text("class Svc:\n    def known(self): pass\n")
    callee = _callable(KNOWN, "pkg/svc.py", 2, 2)
    callee[cs.KEY_LABEL] = callee_label
    graph = _Graph(
        [_callable(RUN, "pkg/app.py", 1, 2), callee],
        [{cs.KEY_FROM_QN: RUN, cs.KEY_TO_QN: KNOWN}],
    )
    trace = _trace(
        tmp_path / "t.jsonl",
        repo,
        [(("pkg/app.py", "run", 1), ("pkg/svc.py", "Svc.known", 2))],
    )
    # The shared write-back fake answers only the reads ingest makes.
    store = cast(TraceGraphProtocol, graph)
    assert ingest_trace(trace, store, repo, PROJECT).confirmed_static == 1
    return graph.writes


@pytest.mark.parametrize("callee_label", ["Method", "Function"])
def test_a_confirmation_matches_both_endpoints_by_label(
    tmp_path: Path, callee_label: str
) -> None:
    ((query, params),) = _confirmations(tmp_path, callee_label)
    assert query.lstrip().startswith(
        "MATCH (a:Function {qualified_name: $from_qn})"
        f"-[r:CALLS]->(b:{callee_label} {{qualified_name: $to_qn}})"
    ), query
    assert params == {
        cs.KEY_FROM_QN: RUN,
        cs.KEY_TO_QN: KNOWN,
        cs.KEY_RESOLUTION: cs.EdgeResolution.TRACE_CONFIRMED,
    }


def test_a_confirmation_still_leaves_trace_only_edges_alone(tmp_path: Path) -> None:
    # Negative: the static-only filter survives the labelling.
    ((query, _params),) = _confirmations(tmp_path, "Method")
    assert "coalesce(r.static_missed, false) = false" in query


@pytest.mark.parametrize(
    "label",
    ["Class", "Function)-[x]-(y", "Function {qualified_name: 'a'}", ""],
    ids=["not-callable", "injected-pattern", "injected-map", "empty"],
)
def test_only_a_callable_label_is_formatted_into_the_query(label: str) -> None:
    # Negative: labels cannot be Cypher parameters, so the builder accepts
    # only the three callable labels the trace resolves against.
    from codebase_rag.cypher_queries import build_trace_confirm_calls_query

    with pytest.raises(ValueError, match="label"):
        build_trace_confirm_calls_query("Function", label)
    with pytest.raises(ValueError, match="label"):
        build_trace_confirm_calls_query(label, "Function")
