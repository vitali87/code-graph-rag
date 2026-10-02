# Real-Memgraph check of `implementors` / `overrides` with `depth` (issue
# #2624): the unit tests drive a fake that answers the hop queries in Python,
# so only a real index proves the INHERITS / IMPLEMENTS / OVERRIDES edges the
# parsers write carry what the walk reads, and that the hop queries run on
# the Memgraph the stack ships.
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import graph_query
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

PROJECT = "hier"

_ADD_EDGE = "    def add_edge(self, u, v):\n        return (u, v)\n"
_JAVA_VALIDATE = "    public boolean validate(T t) { return true; }\n"

FILES = {
    # networkx-shaped: MultiDiGraph(MultiGraph, DiGraph) is a Graph two
    # hops down, and its add_edge overrides MultiGraph.add_edge.
    "nx/__init__.py": "",
    "nx/graph.py": f"class Graph:\n{_ADD_EDGE}",
    "nx/digraph.py": f"from nx.graph import Graph\n\n\nclass DiGraph(Graph):\n{_ADD_EDGE}",
    "nx/multigraph.py": "from nx.graph import Graph\n\n\n"
    f"class MultiGraph(Graph):\n{_ADD_EDGE}",
    "nx/multidigraph.py": "from nx.digraph import DiGraph\n"
    "from nx.multigraph import MultiGraph\n\n\n"
    f"class MultiDiGraph(MultiGraph, DiGraph):\n{_ADD_EDGE}",
    # FluentValidation-shaped: every validator implements the interface
    # through the abstract base.
    "fv/IValidator.java": "package fv;\n\npublic interface IValidator<T> {\n"
    "    boolean validate(T t);\n}\n",
    "fv/AbstractValidator.java": "package fv;\n\n"
    "public abstract class AbstractValidator<T> implements IValidator<T> {\n"
    f"{_JAVA_VALIDATE}}}\n",
    "fv/InlineValidator.java": "package fv;\n\n"
    "public class InlineValidator<T> extends AbstractValidator<T> {\n}\n",
    "fv/PersonValidator.java": "package fv;\n\n"
    "public class PersonValidator extends AbstractValidator<String> {\n}\n",
    "fv/TestValidator.java": "package fv;\n\n"
    "public class TestValidator extends InlineValidator<String> {\n}\n",
}


@pytest.fixture
def indexed(memgraph_ingestor: MemgraphIngestor, tmp_path: Path) -> MemgraphIngestor:
    repo = tmp_path / PROJECT
    for rel, text in FILES.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    ).run()
    return memgraph_ingestor


def _short(qn: str) -> str:
    return qn.removeprefix(f"{PROJECT}.")


def _names(rows: Sequence[Mapping[str, object]]) -> set[str]:
    return {_short(str(r["qualified_name"])) for r in rows}


def _hops(rows: Sequence[Mapping[str, object]]) -> set[tuple[object, str, str]]:
    return {
        (r["depth"], _short(str(r["qualified_name"])), _short(str(r["through"])))
        for r in rows
    }


def test_implementors_depth_reaches_the_diamond_subclass(
    indexed: MemgraphIngestor,
) -> None:
    target = f"{PROJECT}.nx.graph.Graph"
    direct = graph_query.implementors(indexed.fetch_all, PROJECT, target)
    assert _names(direct) == {"nx.digraph.DiGraph", "nx.multigraph.MultiGraph"}
    rows = graph_query.implementors(indexed.fetch_all, PROJECT, target, depth=2)
    assert _hops(rows) == {
        (1, "nx.digraph.DiGraph", "nx.graph.Graph"),
        (1, "nx.multigraph.MultiGraph", "nx.graph.Graph"),
        (2, "nx.multidigraph.MultiDiGraph", "nx.digraph.DiGraph"),
        (2, "nx.multidigraph.MultiDiGraph", "nx.multigraph.MultiGraph"),
    }


def test_overrides_depth_reaches_the_transitive_override(
    indexed: MemgraphIngestor,
) -> None:
    target = f"{PROJECT}.nx.graph.Graph.add_edge"
    direct = graph_query.overrides(indexed.fetch_all, PROJECT, target)
    assert _names(direct) == {
        "nx.digraph.DiGraph.add_edge",
        "nx.multigraph.MultiGraph.add_edge",
    }
    rows = graph_query.overrides(indexed.fetch_all, PROJECT, target, depth=3)
    assert _hops(rows) == {
        (1, "nx.digraph.DiGraph.add_edge", "nx.graph.Graph.add_edge"),
        (1, "nx.multigraph.MultiGraph.add_edge", "nx.graph.Graph.add_edge"),
        (
            2,
            "nx.multidigraph.MultiDiGraph.add_edge",
            "nx.multigraph.MultiGraph.add_edge",
        ),
    }


def test_overrides_depth_never_turns_back_down_into_a_sibling(
    indexed: MemgraphIngestor,
) -> None:
    target = f"{PROJECT}.nx.multigraph.MultiGraph.add_edge"
    rows = graph_query.overrides(indexed.fetch_all, PROJECT, target, depth=3)
    # Up to Graph.add_edge, down to MultiDiGraph.add_edge, and not across to
    # DiGraph.add_edge, which also overrides Graph.add_edge.
    assert _hops(rows) == {
        (1, "nx.graph.Graph.add_edge", "nx.multigraph.MultiGraph.add_edge"),
        (
            1,
            "nx.multidigraph.MultiDiGraph.add_edge",
            "nx.multigraph.MultiGraph.add_edge",
        ),
    }


def test_implementors_depth_reaches_every_validator_through_the_base(
    indexed: MemgraphIngestor,
) -> None:
    target = next(
        r["qualified_name"]
        for r in graph_query.resolve(indexed.fetch_all, PROJECT, "IValidator")
        if r["label"] == "Interface"
    )
    direct = graph_query.implementors(indexed.fetch_all, PROJECT, target)
    assert {qn.rsplit(".", 1)[-1] for qn in _names(direct)} == {"AbstractValidator"}
    rows = graph_query.implementors(indexed.fetch_all, PROJECT, target, depth=3)
    assert {
        (r["depth"], _short(r["qualified_name"]).rsplit(".", 1)[-1]) for r in rows
    } == {
        (1, "AbstractValidator"),
        (2, "InlineValidator"),
        (2, "PersonValidator"),
        (3, "TestValidator"),
    }
