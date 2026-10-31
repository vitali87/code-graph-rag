# A TS `type input<T>` and `function input` are two nodes under one qualified
# name, told apart by label (issue #2520). The unit tests record batches; this
# one checks the real store: that label-scoped uniqueness keeps both nodes,
# that every CALLS row lands on the Function, and that a rename of the
# function, whose own return annotation names the type, is planned rather
# than refused.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.rename import rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

SHAPE_TS = """export type input<T> = { value: T };

export function input<T>(value: T): input<T> {
  return { value };
}
"""

USE_TS = """import { input } from "./shape";

export function run(): number {
  return input(1).value;
}

export function each(): number[] {
  return [1, 2].map(input).map((box) => box.value);
}
"""


def _index(ingestor: MemgraphIngestor, root: Path) -> str:
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=ingestor, repo_path=root, parsers=parsers, queries=queries
    )
    updater.run()
    return updater.project_name


@pytest.fixture
def twin_repo(tmp_path: Path) -> Path:
    root = tmp_path / "tsdeclmerge"
    (root / "src").mkdir(parents=True)
    (root / "src" / "shape.ts").write_text(SHAPE_TS)
    (root / "src" / "use.ts").write_text(USE_TS)
    return root


def test_type_and_function_are_two_nodes_and_calls_hit_the_function(
    memgraph_ingestor: MemgraphIngestor, twin_repo: Path
) -> None:
    project = _index(memgraph_ingestor, twin_repo)
    qn = f"{project}.src.shape.input"

    labels = {
        row["label"]
        for row in memgraph_ingestor.fetch_all(
            "MATCH (n) WHERE n.qualified_name = $qn RETURN labels(n)[0] AS label",
            {"qn": qn},
        )
    }
    assert labels == {cs.NodeLabel.FUNCTION.value, cs.NodeLabel.TYPE.value}

    calls = memgraph_ingestor.fetch_all(
        "MATCH (a)-[r:CALLS]->(b) WHERE b.qualified_name STARTS WITH $qn "
        "RETURN a.name AS caller, labels(b)[0] AS label, "
        "b.qualified_name AS target, r.resolution AS resolution",
        {"qn": qn},
    )
    assert {row["caller"] for row in calls} == {"run", "each"}
    assert {(row["label"], row["target"], row["resolution"]) for row in calls} == {
        (cs.NodeLabel.FUNCTION.value, qn, cs.EdgeResolution.EXACT.value)
    }


def test_renaming_the_function_leaves_the_type_and_its_annotation(
    memgraph_ingestor: MemgraphIngestor, twin_repo: Path
) -> None:
    project = _index(memgraph_ingestor, twin_repo)

    report = rename(
        twin_repo,
        memgraph_ingestor.fetch_all,
        project,
        f"{project}.src.shape.input",
        "makeInput",
        dry_run=True,
    )

    assert "+export function makeInput<T>(value: T): input<T> {" in report.diff
    assert "-export type" not in report.diff
    assert "+export type" not in report.diff
    assert '+import { makeInput } from "./shape";' in report.diff
    assert "+  return makeInput(1).value;" in report.diff
