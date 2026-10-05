# Real-Memgraph check of issue #2471: the C# entry-point rule reads the
# method's modifiers, return type and parameter types, so the dead-code node
# query has to return them. Indexes a non-public `static Main` and runs the
# same collection `cgr dead-code` does.
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from codebase_rag import constants as cs
from codebase_rag.dead_code import collect_dead_code, default_dead_code_config
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_PROGRAM = """\
using System.Threading.Tasks;
namespace App;
class Program
{
    static async Task Main(string[] args) { Helper(); await Task.Yield(); }
    static void Helper() { }
    static void Orphan() { }
}
class Worker
{
    void Main() { Chore(); }
    void Chore() { }
}
"""


def test_non_public_main_roots_its_helper(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    pytest.importorskip("tree_sitter_c_sharp")
    repo = tmp_path / "csmain"
    repo.mkdir()
    (repo / "Program.cs").write_text(_PROGRAM, encoding="utf-8")
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=memgraph_ingestor, repo_path=repo, parsers=parsers, queries=queries
    ).run()
    memgraph_ingestor.flush_all()

    config = default_dead_code_config(include_tests=True, include_classes=False)
    rows = collect_dead_code(memgraph_ingestor, "csmain", config)
    dead = {
        ".".join(str(row[cs.KEY_QUALIFIED_NAME]).split("(", 1)[0].rsplit(".", 2)[-2:])
        for row in rows
    }

    # Negative: the orphan and an instance Main with its callee stay reported.
    assert dead == {"Program.Orphan", "Worker.Main", "Worker.Chore"}, dead
