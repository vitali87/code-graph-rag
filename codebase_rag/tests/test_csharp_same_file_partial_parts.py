"""Two partial declarations of one C# class in the same file share one
partial group, the same as parts split across files (issue #2014)."""

from __future__ import annotations

from pathlib import Path

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

# The field is declared on the SECOND part; the decoy carries the same
# member name so the bare-name fallback has a wrong answer to give.
SOURCE = (
    "namespace App;\n\n"
    "public class Widget\n{\n    public void Go() { }\n}\n\n"
    "public class Decoy\n{\n    public void Go() { }\n}\n\n"
    "public partial class Bench\n{\n"
    "    public void Run()\n    {\n        _widget.Go();\n    }\n}\n\n"
    "public partial class Bench\n{\n"
    "    private Widget _widget = new Widget();\n}\n"
)


def test_a_field_declared_on_the_other_same_file_part_types_its_receiver(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.cs").write_text(SOURCE, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    calls = {
        str(target)
        for _sl, source, kind, _tl, target in store.edges
        if ".Bench.Run" in str(source) and kind == "CALLS"
    }
    assert "proj.src.App.App.Widget.Go" in calls, sorted(calls)
    assert "proj.src.App.App.Decoy.Go" not in calls, sorted(calls)


# After the local review: a verbatim identifier opens with the marker's
# character, and stripping at it emptied the name, so every verbatim-named
# partial type in a directory merged into one group.
VERBATIM = (
    "namespace App;\n\n"
    "public class Widget\n{\n    public void Go() { }\n}\n\n"
    "public class Decoy\n{\n    public void Go() { }\n}\n\n"
    "public partial class @lock\n{\n    private Widget _w = new Widget();\n}\n\n"
    "public partial class @event\n{\n    private Decoy _w = new Decoy();\n"
    "    public void Run() { this._w.Go(); }\n}\n"
)


def test_verbatim_named_partial_types_stay_apart(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "App.cs").write_text(VERBATIM, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    calls = {
        str(target)
        for _sl, source, kind, _tl, target in store.edges
        if "@event.Run" in str(source) and kind == "CALLS"
    }
    assert "proj.src.App.App.Decoy.Go" in calls, sorted(calls)
    assert "proj.src.App.App.Widget.Go" not in calls, sorted(calls)
