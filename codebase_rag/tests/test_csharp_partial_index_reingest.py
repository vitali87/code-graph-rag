"""A reused updater keeps each C# partial part once in its partial group:
re-parsing a part does not append it again, and deleting a part's file
drops it from the group (issue #2016)."""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FILES = {
    "src/App.cs": (
        "namespace App;\n\n"
        "public class Widget\n{\n    public void Go() { }\n}\n\n"
        "public partial class Bench\n{\n"
        "    public void Run() { _widget.Go(); }\n}\n"
    ),
    "src/BenchBase.cs": (
        "namespace App;\n\n"
        "public partial class Bench\n{\n"
        "    private Widget _widget = new Widget();\n}\n"
    ),
}

PART_A = "proj.src.App.App.Bench"
PART_B = "proj.src.BenchBase.App.Bench"


def _updater(root: Path) -> GraphUpdater:
    for rel, source in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    if cs.SupportedLanguage.CSHARP not in parsers:
        pytest.skip("csharp parser not available")
    return GraphUpdater(
        ingestor=_StatefulIngestor(),  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    )


def _groups(updater: GraphUpdater) -> dict[str, list[str]]:
    return updater.factory.definition_processor.csharp_partial_groups


def test_a_re_parsed_part_is_kept_once(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    updater = _updater(root)
    updater.run()
    assert sorted(_groups(updater)[PART_A]) == [PART_A, PART_B]
    updater.reingest([root / "src" / "BenchBase.cs"])
    assert sorted(_groups(updater)[PART_A]) == [PART_A, PART_B], _groups(updater)
    assert _groups(updater)[PART_B] is _groups(updater)[PART_A]


def test_a_deleted_part_leaves_the_group(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    updater = _updater(root)
    updater.run()
    (root / "src" / "BenchBase.cs").unlink()
    updater.reingest([root / "src" / "BenchBase.cs"])
    assert PART_B not in _groups(updater), sorted(_groups(updater))
    assert _groups(updater)[PART_A] == [PART_A], _groups(updater)
