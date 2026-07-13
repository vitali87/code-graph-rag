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


def _build(
    ingestor: MemgraphIngestor, tmp_path: Path, filename: str, code: str
) -> None:
    project = tmp_path / "flow_js"
    project.mkdir()
    (project / filename).write_text(code, encoding="utf-8")
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


def test_env_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n"
        "  const secret = process.env.SECRET;\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_direct_env_argument_flows_to_stdout(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n  console.log(process.env.TOKEN);\n}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::TOKEN",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_network_read_flows_to_file(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function sync() {\n"
        "  const data = fetch('https://api.example.com/x');\n"
        "  fs.writeFileSync('out.txt', data);\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::NETWORK::https://api.example.com/x",
        "resource::FILE::out.txt",
        kind=FlowKind.RESOURCE.value,
    )


def test_tainted_value_arg_edge_to_callee(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function leak(x) {}\n\n\n"
        "function boot() {\n"
        "  const secret = process.env.SECRET;\n"
        "  leak(secret);\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "app.boot",
        "app.leak",
        kind=FlowKind.ARG.value,
    )


def test_untainted_value_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n  const s = 'safe';\n  console.log(s);\n}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


def test_return_taint_flows_across_call(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) getSecret() returns process.env.SECRET; the caller assigns and logs it,
    # (H) so the return edge and the ENV -> STDOUT flow both appear (shared fixpoint).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function getSecret() {\n  return process.env.SECRET;\n}\n\n\n"
        "function boot() {\n  const s = getSecret();\n  console.log(s);\n}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert _has(flows, "app.getSecret", "app.boot", kind=FlowKind.RETURN.value)
    assert _has(
        flows,
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_shadowed_console_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A local `console` object is not the global; logging through it must not
    # (H) emit a resource flow.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n"
        "  const console = {};\n"
        "  const s = process.env.SECRET;\n"
        "  console.log(s);\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(
        (f["to"] or "").endswith("STDOUT::<dynamic>")
        and f["kind"] == FlowKind.RESOURCE.value
        for f in flows
    )


def test_comment_in_arguments_does_not_break_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Comments are named children; they must not shift argument indices or hide
    # (H) the taint of a directly-passed source.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n  console.log(/* note */ process.env.SECRET);\n}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
        kind=FlowKind.RESOURCE.value,
    )


def test_comment_before_sink_target_keeps_identity(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A comment before the sink's target argument must not shift the positional
    # (H) index used for the resource identity (literal_target filters comments).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function sync() {\n"
        "  const data = fetch('https://api.example.com/x');\n"
        "  fs.writeFileSync(/* path */ 'out.txt', data);\n"
        "}\n",
    )
    assert _has(
        _flows(memgraph_ingestor),
        "resource::NETWORK::https://api.example.com/x",
        "resource::FILE::out.txt",
        kind=FlowKind.RESOURCE.value,
    )


def test_module_import_plus_local_shadow_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) `fs` is imported module-wide, but a function-local `const fs = {}` shadows
    # (H) it: fs.writeFileSync must not be the builtin, so no resource flow appears.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "import fs from 'fs';\n\n\n"
        "function f() {\n"
        "  const fs = {};\n"
        "  const data = fetch('u');\n"
        "  fs.writeFileSync('out', data);\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(
        (f["to"] or "").endswith("FILE::out") and f["kind"] == FlowKind.RESOURCE.value
        for f in flows
    )


def test_typescript_typed_param_shadows_source(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) A TS parameter is a required_parameter wrapper, not a bare identifier; a
    # (H) `process` parameter must still shadow the global env source.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.ts",
        "function run(process: any) {\n"
        "  const a = process.env.SECRET;\n"
        "  console.log(a);\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)
