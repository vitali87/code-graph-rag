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

_FLOWS = cs.RelationshipType.FLOWS_TO.value


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "flow_java"
    project.mkdir()
    (project / "App.java").write_text(code, encoding="utf-8")
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
        f"MATCH (a)-[r:{_FLOWS}]->(b) "
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


def test_java_env_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A local carries the env source to a later println: ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void leak() {\n"
        '        String s = System.getenv("SECRET");\n'
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_java_direct_env_argument_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) The env read nested directly in the print call still flows ENV -> STDOUT.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void leak() {\n"
        '        System.out.println(System.getenv("TOKEN"));\n'
        "    }\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::TOKEN",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_java_tainted_value_arg_edge_to_callee(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Passing a tainted local to a first-party method emits a caller->callee ARG edge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void leak() {\n"
        '        String s = System.getenv("SECRET");\n'
        "        sink(s);\n"
        "    }\n"
        "    void sink(String v) {}\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "leak()",
        "sink(String)",
        kind=FlowKind.ARG.value,
    )


def test_java_return_taint_edge(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A method returning a tainted value emits a callee->caller RETURN edge to its
    # (H) caller (resolved cross-method through the shared return-taint fixpoint).
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    String read() {\n"
        '        return System.getenv("SECRET");\n'
        "    }\n"
        "    void use() {\n"
        "        String s = read();\n"
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "read()",
        "use()",
        kind=FlowKind.RETURN.value,
    )


def test_java_reassignment_kills_taint(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Overwriting the tainted local with a clean literal before the print kills the
    # (H) taint, so no ENV -> STDOUT flow survives (Java assignment_expression bind).
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void scrub() {\n"
        '        String s = System.getenv("SECRET");\n'
        '        s = "redacted";\n'
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert not any(
        f["kind"] == FlowKind.RESOURCE.value for f in _flows(memgraph_ingestor)
    )


def test_java_untainted_local_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A println of a plain local (no source) emits no resource flow.
    _build(
        memgraph_ingestor,
        tmp_path,
        "class App {\n"
        "    void quiet() {\n"
        '        String s = "literal";\n'
        "        System.out.println(s);\n"
        "    }\n"
        "}\n",
    )
    assert not any(
        f["kind"] == FlowKind.RESOURCE.value for f in _flows(memgraph_ingestor)
    )
