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


def _build(ingestor: MemgraphIngestor, tmp_path: Path, code: str) -> None:
    project = tmp_path / "flow_project"
    project.mkdir()
    (project / "flow.py").write_text(code, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=project,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()


def _resource_flow_present(ingestor: MemgraphIngestor, frm: str, to: str) -> bool:
    rows = ingestor.fetch_all(
        f"MATCH (a:{cs.NodeLabel.RESOURCE.value})-[r:{cs.RelationshipType.FLOWS_TO.value}]->"
        f"(b:{cs.NodeLabel.RESOURCE.value}) "
        "WHERE r.kind = $kind RETURN a.qualified_name AS frm, b.qualified_name AS to",
        {"kind": FlowKind.RESOURCE.value},
    )
    return any(str(row["frm"]) == frm and str(row["to"]) == to for row in rows)


def test_kill_on_one_branch_still_flows(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) x tainted, killed on ONLY the if-branch; the else-path (implicit) still
    # (H) leaks ENV::K -> STDOUT, so the flow must survive (MAY analysis).
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def f(cond):\n"
        "    x = os.getenv('K')\n"
        "    if cond:\n"
        "        x = 'safe'\n"
        "    print(x)\n",
    )
    assert _resource_flow_present(memgraph_ingestor, ENV_K, STDOUT)


def test_taint_on_one_branch_flows(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) x clean, tainted on ONLY the if-branch; that path leaks, so the flow
    # (H) must appear (taint that exists on ANY path survives the merge).
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def f(cond):\n"
        "    x = 'safe'\n"
        "    if cond:\n"
        "        x = os.getenv('K')\n"
        "    print(x)\n",
    )
    assert _resource_flow_present(memgraph_ingestor, ENV_K, STDOUT)


def test_kill_on_all_branches_does_not_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) x tainted, then reassigned to an untainted value on EVERY path (if AND
    # (H) else): killed on all paths, so NO ENV::K -> STDOUT flow may remain.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def f(cond):\n"
        "    x = os.getenv('K')\n"
        "    if cond:\n"
        "        x = 'a'\n"
        "    else:\n"
        "        x = 'b'\n"
        "    print(x)\n",
    )
    assert not _resource_flow_present(memgraph_ingestor, ENV_K, STDOUT)


def test_loop_carried_taint_reaches_earlier_statement(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) x is tainted at the END of the loop body; on the NEXT iteration the
    # (H) earlier print(x) sees that taint. The two-pass loop merge must catch it.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def f(items):\n"
        "    x = 'safe'\n"
        "    for i in items:\n"
        "        print(x)\n"
        "        x = os.getenv('K')\n",
    )
    assert _resource_flow_present(memgraph_ingestor, ENV_K, STDOUT)


def test_taint_survives_except_only_kill(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    # (H) x killed only on the except path; the normal (no-exception) path keeps
    # (H) the taint, so the flow must survive the try/except merge.
    _build(
        memgraph_ingestor,
        tmp_path,
        "import os\n\n\n"
        "def risky():\n    pass\n\n\n"
        "def f():\n"
        "    x = os.getenv('K')\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        x = 'safe'\n"
        "    print(x)\n",
    )
    assert _resource_flow_present(memgraph_ingestor, ENV_K, STDOUT)
