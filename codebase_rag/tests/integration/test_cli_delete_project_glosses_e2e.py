# Real-Memgraph check of issue #3231: `cgr delete-project` left the project's
# glosses behind as edgeless EXACT notes, and indexing the same checkout again
# re-attached them by name. Only the MCP `delete_project` tool swept them.
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import gloss
from codebase_rag.cli import app
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

if TYPE_CHECKING:
    from codebase_rag.services.graph_service import MemgraphIngestor

pytestmark = [pytest.mark.integration]

_HELPER = "def helper(x):\n    return x + 1\n"


def _index(ingestor: MemgraphIngestor, root: Path, project: str) -> None:
    parsers, queries = load_parsers()
    GraphUpdater(
        ingestor=ingestor,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    ).run(force=True)


def _annotate(ingestor: MemgraphIngestor, project: str, body: str) -> None:
    row = gloss.write_gloss(
        ingestor.fetch_all,
        ingestor.execute_write,
        project,
        f"{project}.a.helper",
        body,
        cs.GlossKind.INVARIANT,
    )
    assert row.get(cs.KEY_ANCHOR_STATE) == cs.GlossAnchorState.EXACT, row


def _notes(ingestor: MemgraphIngestor, project: str) -> list[str]:
    rows = ingestor.fetch_all(
        "MATCH (g:Gloss {project: $p}) RETURN g.body AS body", {"p": project}
    )
    return sorted(str(row["body"]) for row in rows)


def _delete(ingestor: MemgraphIngestor, project: str) -> str:
    with (
        patch("codebase_rag.cli.connect_memgraph", return_value=nullcontext(ingestor)),
        patch("codebase_rag.cli.delete_project_embeddings"),
    ):
        result = CliRunner().invoke(app, ["delete-project", "--name", project])
    assert result.exit_code == 0, result.output
    return result.output


def test_cli_delete_project_removes_the_projects_glosses(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    for project in ("gd", "keep"):
        (tmp_path / project).mkdir()
        (tmp_path / project / "a.py").write_text(_HELPER, encoding="utf-8")
        _index(memgraph_ingestor, tmp_path / project, project)
        _annotate(memgraph_ingestor, project, f"{project}: callers rely on +1")

    _delete(memgraph_ingestor, "gd")

    assert _notes(memgraph_ingestor, "gd") == []
    # Negative: another project's note is untouched.
    assert _notes(memgraph_ingestor, "keep") == ["keep: callers rely on +1"]


def test_reindexing_after_the_delete_starts_with_no_notes(
    memgraph_ingestor: MemgraphIngestor, tmp_path: Path
) -> None:
    (tmp_path / "a.py").write_text(_HELPER, encoding="utf-8")
    _index(memgraph_ingestor, tmp_path, "gd")
    _annotate(memgraph_ingestor, "gd", "callers rely on +1")

    _delete(memgraph_ingestor, "gd")
    _index(memgraph_ingestor, tmp_path, "gd")

    result = gloss.glosses_for(memgraph_ingestor.fetch_all, "gd", "gd.a.helper")
    assert result.get("annotating") == [], result
