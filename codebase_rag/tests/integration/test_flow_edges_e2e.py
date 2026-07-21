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

# One module exercising every I/O and data-flow shape end to end:
#   os.getenv -> READS_FROM ENV; print -> WRITES_TO STDOUT;
#   x = getenv('K'); print(x)        -> resource->resource (ENV::K -> STDOUT)
#   t = getenv('T'); forward(t)      -> arg flow (leak -> forward, via=arg:0)
#   r = build(); print(r)            -> return flow (build -> leak) + the
#                                       returned read reaches STDOUT.
FLOW_CODE = """\
import os


def build():
    return os.getenv("K")


def forward(v):
    print(v)


def leak():
    x = os.getenv("K")
    print(x)
    t = os.getenv("T")
    forward(t)
    r = build()
    print(r)
"""

ENV_K = "resource::ENV::K"
STDOUT = "resource::STDOUT::<dynamic>"


@pytest.fixture
def flow_project(tmp_path: Path) -> Path:
    project = tmp_path / "flow_project"
    project.mkdir()
    (project / "flow.py").write_text(FLOW_CODE, encoding="utf-8")
    return project


def _index(ingestor: MemgraphIngestor, project: Path, *, io: bool) -> None:
    parsers, queries = load_parsers()
    capture = resolve_capture([cs.CaptureGroup.IO.value]) if io else None
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=capture,
    ).run()


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, code: str, *, io: bool = True
) -> None:
    project = tmp_path / "flow_project"
    project.mkdir()
    (project / "flow.py").write_text(code, encoding="utf-8")
    _index(ingestor, project, io=io)


def _resource_qns(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all(
        f"MATCH (n:{cs.NodeLabel.RESOURCE.value}) RETURN n.qualified_name AS qn"
    )
    return {str(row["qn"]) for row in rows}


def _rel_types(ingestor: MemgraphIngestor) -> set[str]:
    rows = ingestor.fetch_all("MATCH ()-[r]->() RETURN DISTINCT type(r) AS t")
    return {str(row["t"]) for row in rows}


def _flows(ingestor: MemgraphIngestor) -> list[dict[str, str | None]]:
    rows = ingestor.fetch_all(
        f"MATCH (a)-[r:{cs.RelationshipType.FLOWS_TO.value}]->(b) "
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, "
        "r.kind AS kind, r.via AS via"
    )
    return [
        {key: None if value is None else str(value) for key, value in row.items()}
        for row in rows
    ]


def _has(flows: list[dict[str, str | None]], frm: str, to: str, **props: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and all(f.get(k) == v for k, v in props.items())
        for f in flows
    )


def test_io_and_flow_edges_survive_the_memgraph_round_trip(
    memgraph_ingestor: MemgraphIngestor, flow_project: Path
) -> None:
    _index(memgraph_ingestor, flow_project, io=True)

    # Resource endpoints exist (no dangling FLOWS_TO edge).
    resources = _resource_qns(memgraph_ingestor)
    assert ENV_K in resources
    assert STDOUT in resources

    # READS_FROM / WRITES_TO landed alongside FLOWS_TO.
    types = _rel_types(memgraph_ingestor)
    assert cs.RelationshipType.READS_FROM.value in types
    assert cs.RelationshipType.WRITES_TO.value in types
    assert cs.RelationshipType.FLOWS_TO.value in types

    flows = _flows(memgraph_ingestor)
    # resource -> resource: the env value reaches stdout.
    assert _has(flows, ENV_K, STDOUT, kind=FlowKind.RESOURCE.value)
    # arg flow: a tainted local passed into forward().
    assert _has(
        flows, "flow.leak", "flow.forward", kind=FlowKind.ARG.value, via="arg:0"
    )
    # return flow: build() returns a tainted value into leak().
    assert _has(
        flows, "flow.build", "flow.leak", kind=FlowKind.RETURN.value, via="return"
    )


def test_default_capture_writes_no_resource_or_flow(
    memgraph_ingestor: MemgraphIngestor, flow_project: Path
) -> None:
    _index(memgraph_ingestor, flow_project, io=False)

    assert _resource_qns(memgraph_ingestor) == set()
    assert cs.RelationshipType.FLOWS_TO.value not in _rel_types(memgraph_ingestor)


def test_method_body_flow_uses_a_method_caller(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # The caller_spec inside a class body is a Method, not a Function; the
    # resource->resource edge must still round-trip from a method body.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "class Loader:\n"
        "    def load(self):\n"
        "        v = os.getenv('SECRET')\n"
        "        print(v)\n",
    )
    types = _rel_types(memgraph_ingestor)
    assert cs.RelationshipType.READS_FROM.value in types
    assert cs.RelationshipType.WRITES_TO.value in types
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        STDOUT,
        kind=FlowKind.RESOURCE.value,
    )


def test_multi_hop_assignment_chain_reaches_sink(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Taint must survive a chain of plain-identifier reassignments.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def leak():\n"
        "    a = os.getenv('K')\n"
        "    b = a\n"
        "    c = b\n"
        "    print(c)\n",
    )
    assert _has(_flows(memgraph_ingestor), ENV_K, STDOUT, kind=FlowKind.RESOURCE.value)


def test_retaint_tracks_latest_source_only(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # Overwriting a tainted local with a *different* source rebinds taint:
    # the sink sees the second source, never the discarded first.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def leak():\n"
        "    x = os.getenv('A')\n"
        "    x = os.getenv('B')\n"
        "    print(x)\n",
    )
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "resource::ENV::B", STDOUT, kind=FlowKind.RESOURCE.value)
    assert not _has(flows, "resource::ENV::A", STDOUT)


def test_transitive_two_hop_return_carries_source(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A source returned through two nested call boundaries keeps its origin
    # resource, so the outermost sink emits the full resource flow AND every hop
    # of the callee->caller return chain is present (direct-return `return
    # inner()` emits its edge like an assigned `v = outer()`). Callees precede
    # callers (single-pass, source-ordered).
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def inner():\n    return os.getenv('DEEP')\n\n\n"
        "def outer():\n    return inner()\n\n\n"
        "def top():\n    v = outer()\n    print(v)\n",
    )
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "resource::ENV::DEEP", STDOUT, kind=FlowKind.RESOURCE.value)
    assert _has(flows, "flow.inner", "flow.outer", kind=FlowKind.RETURN.value)
    assert _has(flows, "flow.outer", "flow.top", kind=FlowKind.RETURN.value)


def test_nested_scope_taint_does_not_leak_to_outer(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # A nested def is a scope boundary: its `x = getenv(...)` must not taint
    # the outer function's same-named `x`. The nested read still exists as its
    # own READS_FROM, but no ENV::NESTED -> STDOUT flow may be attributed here.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def outer():\n"
        "    def inner():\n"
        "        x = os.getenv('NESTED')\n"
        "        return x\n"
        "    x = 2\n"
        "    print(x)\n",
    )
    assert "resource::ENV::NESTED" in _resource_qns(memgraph_ingestor)
    assert not _has(_flows(memgraph_ingestor), "resource::ENV::NESTED", STDOUT)


def test_cooccurrence_is_not_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # An unrelated read next to an untainted call is not data flow: no arg
    # edge to the callee and no resource flow to the sink.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def helper(v):\n    pass\n\n\n"
        "def caller():\n"
        "    u = 1\n"
        "    helper(u)\n"
        "    os.getenv('K')\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any((f["to"] or "").endswith("flow.helper") for f in flows)
    assert not _has(flows, ENV_K, STDOUT)


def test_go_loop_carried_flow_survives_round_trip(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # issue #714 lean-flow depth: a later loop iteration carries the ENV read
    # back to the file write of an earlier statement (two-pass loop walk).
    project = tmp_path / "go_flow_project"
    project.mkdir()
    (project / "main.go").write_text(
        "package main\n\n"
        'import "os"\n\n'
        "func work(s string) {\n"
        "\tfor i := 0; i < 2; i++ {\n"
        '\t\tos.WriteFile("out.txt", []byte(s), 0644)\n'
        '\t\ts = os.Getenv("SECRET")\n'
        "\t}\n"
        "}\n",
        encoding="utf-8",
    )
    _index(memgraph_ingestor, project, io=True)
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::FILE::out.txt",
    )
