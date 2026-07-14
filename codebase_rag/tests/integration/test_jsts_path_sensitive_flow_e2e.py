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
    project = tmp_path / "flow_ps"
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
        "RETURN a.qualified_name AS frm, b.qualified_name AS to, r.kind AS kind"
    )
    return [{k: None if v is None else str(v) for k, v in row.items()} for row in rows]


def _has(flows: list[dict[str, str | None]], frm: str, to: str, **props: str) -> bool:
    return any(
        (f["frm"] or "").endswith(frm)
        and (f["to"] or "").endswith(to)
        and all(f.get(k) == v for k, v in props.items())
        for f in flows
    )


def _has_resource(flows: list[dict[str, str | None]], frm: str, to: str) -> bool:
    return _has(flows, frm, to, kind=FlowKind.RESOURCE.value)


def test_conditional_kill_survives_skip_path(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) MAY: the kill happens only on the if-path, so the skip path keeps the taint;
    # (H) after the merge `secret` is still tainted and the flow must survive. The flat
    # (H) walk wrongly applied the kill unconditionally (false negative).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot(cond) {\n"
        "  let secret = process.env.SECRET;\n"
        "  if (cond) { secret = 'safe'; }\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_kill_on_all_branches_no_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Killed on BOTH the if and the else path, so after the merge `secret` carries
    # (H) no taint and no resource flow may appear.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot(cond) {\n"
        "  let secret = process.env.SECRET;\n"
        "  if (cond) { secret = 'a'; } else { secret = 'b'; }\n"
        "  console.log(secret);\n"
        "}\n",
    )
    flows = _flows(memgraph_ingestor)
    assert not any(f["kind"] == FlowKind.RESOURCE.value for f in flows)


def test_taint_introduced_in_one_branch_survives(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Taint introduced on only the if-path still reaches the sink after the merge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot(cond) {\n"
        "  let secret = '';\n"
        "  if (cond) { secret = process.env.SECRET; }\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_else_if_branch_taint_survives(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) An else-if chain: taint introduced in the middle branch survives the merge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot(a, b) {\n"
        "  let secret = '';\n"
        "  if (a) { secret = 'x'; }\n"
        "  else if (b) { secret = process.env.SECRET; }\n"
        "  else { secret = 'y'; }\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_loop_carried_taint_reaches_earlier_sink(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) The write happens BEFORE the tainting assignment in source order, but a later
    # (H) loop iteration carries the taint back to it; the body is walked twice so the
    # (H) NETWORK -> FILE flow is caught. The flat walk missed this (false negative).
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function sync(items) {\n"
        "  let data = '';\n"
        "  for (const it of items) {\n"
        "    fs.writeFileSync('out.txt', data);\n"
        "    data = fetch('https://api.example.com/x');\n"
        "  }\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::NETWORK::https://api.example.com/x",
        "resource::FILE::out.txt",
    )


def test_try_body_taint_survives_to_after(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) Taint assigned in the try body reaches the sink after the try/catch merge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n"
        "  let secret = '';\n"
        "  try { secret = process.env.SECRET; } catch (e) {}\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )


def test_straight_line_still_works(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) No branches: behaves exactly like the old flat walk.
    _build(
        memgraph_ingestor,
        tmp_path,
        "app.js",
        "function boot() {\n"
        "  const secret = process.env.SECRET;\n"
        "  console.log(secret);\n"
        "}\n",
    )
    assert _has_resource(
        _flows(memgraph_ingestor),
        "resource::ENV::SECRET",
        "resource::STDOUT::<dynamic>",
    )
