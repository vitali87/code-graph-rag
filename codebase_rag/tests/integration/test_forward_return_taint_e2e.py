from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.parsers.flow_access import FlowKind

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

ENV_K = "resource::ENV::K"
STDOUT = "resource::STDOUT::<dynamic>"


def _index(ingestor: MemgraphIngestor, project: Path) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _flows(ingestor: MemgraphIngestor) -> list[dict[str, str | None]]:
    rows = ingestor.fetch_all(
        f"MATCH (a)-[r:{cs.RelationshipType.FLOWS_TO.value}]->(b) "
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, "
        "r.kind AS kind, r.via AS via"
    )
    return [{k: None if v is None else str(v) for k, v in row.items()} for row in rows]


def _has(flows: list[dict[str, str | None]], frm: str, to: str, **props: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and all(f.get(k) == v for k, v in props.items())
        for f in flows
    )


def test_forward_return_taint_same_file(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # caller() is defined BEFORE build(), so build's return-taint summary is
    # unknown when caller is walked. The forward return edge and the
    # build-origin resource flow must still appear (fixpoint over summaries).
    project = tmp_path / "fwd_project"
    project.mkdir()
    (project / "mod.py").write_text(
        "import os\n\n\n"
        "def caller():\n"
        "    v = build()\n"
        "    print(v)\n\n\n"
        "def build():\n"
        "    return os.getenv('K')\n",
        encoding="utf-8",
    )
    _index(memgraph_ingestor, project)
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "mod.build", "mod.caller", kind=FlowKind.RETURN.value)
    assert _has(flows, ENV_K, STDOUT, kind=FlowKind.RESOURCE.value)


def test_forward_return_taint_transitive(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # top() -> mid() -> low() with every function defined before the one it
    # calls: the fixpoint must transitively carry ENV::K through both return
    # hops to the STDOUT sink, and emit both return edges.
    project = tmp_path / "fwd_project2"
    project.mkdir()
    (project / "mod.py").write_text(
        "import os\n\n\n"
        "def top():\n"
        "    v = mid()\n"
        "    print(v)\n\n\n"
        "def mid():\n"
        "    return low()\n\n\n"
        "def low():\n"
        "    return os.getenv('K')\n",
        encoding="utf-8",
    )
    _index(memgraph_ingestor, project)
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "mod.low", "mod.mid", kind=FlowKind.RETURN.value)
    assert _has(flows, "mod.mid", "mod.top", kind=FlowKind.RETURN.value)
    assert _has(flows, ENV_K, STDOUT, kind=FlowKind.RESOURCE.value)


def test_untainted_forward_callee_emits_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # compute() (defined later) returns nothing tainted. Deferring caller's
    # taint must NOT fabricate a return edge or an arg edge once the fixpoint
    # resolves compute() to untainted; no FLOWS_TO edge may exist at all.
    project = tmp_path / "clean_project"
    project.mkdir()
    (project / "mod.py").write_text(
        "def caller():\n"
        "    v = compute()\n"
        "    sink(v)\n\n\n"
        "def compute():\n"
        "    return 42\n\n\n"
        "def sink(x):\n"
        "    pass\n",
        encoding="utf-8",
    )
    _index(memgraph_ingestor, project)
    assert _flows(memgraph_ingestor) == []
