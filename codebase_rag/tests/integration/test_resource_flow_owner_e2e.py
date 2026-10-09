"""Issue #2746: a resource-to-resource FLOWS_TO edge goes away with the code
that produced it.

`ENV::K -FLOWS_TO-> STDOUT` was written between two Resource nodes with only
`{kind: "resource"}`: no owner, and one edge shared by every function in every
project that leaked `K`. Neither the module delete of a re-parsed file nor
`delete-project` reaches an edge between two Resources, and the orphan sweep
keeps a component another project anchors (`STDOUT`). So a fixed leak stayed
in the graph through an incremental re-index, a full rebuild and the
deletion of the only project that ever produced it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

TOKEN = "resource::ENV::BILLING_TOKEN"
STDOUT = "resource::STDOUT::<dynamic>"

LEAK = """import os


def show_config():
    token = os.getenv("BILLING_TOKEN")
    print(token)
"""

FIXED = LEAK.replace("print(token)", 'print("config loaded")')

TWO_LEAKS = (
    LEAK
    + """

def audit():
    print(os.getenv("BILLING_TOKEN"))
"""
)

PRINTER = 'def hello():\n    print("hi")\n'


def _index(
    ingestor: MemgraphIngestor, root: Path, project: str, *, force: bool = False
) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run(force=force)


def _write(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _token_flows(ingestor: MemgraphIngestor) -> list[tuple[str, str | None]]:
    rows = ingestor.fetch_all(
        "MATCH (:Resource {qualified_name: $src})-[r:FLOWS_TO]->(b) "
        "RETURN b.qualified_name AS dst, r.scope AS scope",
        {"src": TOKEN},
    )
    return sorted(
        (str(row["dst"]), None if row["scope"] is None else str(row["scope"]))
        for row in rows
    )


def test_a_fixed_leak_loses_its_flow_on_an_incremental_reindex(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": LEAK})
    _index(memgraph_ingestor, root, "billing", force=True)
    assert [dst for dst, _ in _token_flows(memgraph_ingestor)] == [STDOUT]

    _write(root, {"app.py": FIXED})
    _index(memgraph_ingestor, root, "billing")

    assert _token_flows(memgraph_ingestor) == []


def test_a_full_rebuild_drops_a_fixed_leak(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": LEAK})
    _index(memgraph_ingestor, root, "billing", force=True)

    _write(root, {"app.py": FIXED})
    _index(memgraph_ingestor, root, "billing", force=True)

    assert _token_flows(memgraph_ingestor) == []


def test_deleting_the_project_drops_its_flow_while_stdout_stays_anchored(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    _index(
        memgraph_ingestor,
        _write(tmp_path / "other", {"main.py": PRINTER}),
        "other",
        force=True,
    )
    _index(
        memgraph_ingestor,
        _write(tmp_path / "billing", {"app.py": LEAK}),
        "billing",
        force=True,
    )

    memgraph_ingestor.delete_project("billing")

    assert _token_flows(memgraph_ingestor) == []


def test_each_function_owns_its_own_flow(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": TWO_LEAKS})
    _index(memgraph_ingestor, root, "billing", force=True)

    assert _token_flows(memgraph_ingestor) == [
        (STDOUT, "billing.app.audit"),
        (STDOUT, "billing.app.show_config"),
    ]


def test_removing_one_of_two_leaks_keeps_the_other(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": TWO_LEAKS})
    _index(memgraph_ingestor, root, "billing", force=True)

    _write(root, {"app.py": TWO_LEAKS.replace("print(token)", "print(1)")})
    _index(memgraph_ingestor, root, "billing")

    assert _token_flows(memgraph_ingestor) == [(STDOUT, "billing.app.audit")]


# Negative: what must not change.


def test_a_leak_still_in_the_code_survives_a_reindex_of_its_file(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": LEAK})
    _index(memgraph_ingestor, root, "billing", force=True)

    _write(root, {"app.py": LEAK + "\n\ndef unrelated():\n    return 1\n"})
    _index(memgraph_ingestor, root, "billing")

    assert [dst for dst, _ in _token_flows(memgraph_ingestor)] == [STDOUT]


def test_reindexing_another_file_keeps_the_leak(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    root = _write(tmp_path / "billing", {"app.py": LEAK, "util.py": PRINTER})
    _index(memgraph_ingestor, root, "billing", force=True)

    _write(root, {"util.py": PRINTER + "\n\ndef more():\n    return 2\n"})
    _index(memgraph_ingestor, root, "billing")

    assert [dst for dst, _ in _token_flows(memgraph_ingestor)] == [STDOUT]


def test_deleting_one_project_keeps_another_projects_same_leak(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    for project in ("billing", "payroll"):
        _index(
            memgraph_ingestor,
            _write(tmp_path / project, {"app.py": LEAK}),
            project,
            force=True,
        )

    memgraph_ingestor.delete_project("billing")

    assert [dst for dst, _ in _token_flows(memgraph_ingestor)] == [STDOUT]
