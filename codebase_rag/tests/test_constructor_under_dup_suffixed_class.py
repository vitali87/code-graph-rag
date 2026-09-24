"""A constructor under a duplicate-suffixed class (`Box@8.Box(T)`) is that
class's constructor (issue #2007)."""

from __future__ import annotations

import re
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

FILES = {
    "src/Zeta/Box.cs": (
        "namespace Zeta;\n\npublic class Box\n{\n    public Box() { }\n}\n\n"
        "public class Box<T>\n{\n    public Box(T value) { }\n}\n"
    ),
    "src/App/Use.cs": (
        "using Zeta;\n\nnamespace App;\n\npublic class Use\n{\n"
        "    public void Run() { var b = new Box<int>(1); }\n}\n"
    ),
}


def test_the_generic_twins_constructor_takes_a_calls_edge(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    for rel, source in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    GraphUpdater(
        ingestor=store,  # type: ignore[arg-type]
        repo_path=root,
        parsers=parsers,
        queries=queries,
    ).run()
    # Matched by suffix: the caller's qualified name repeats the namespace on
    # main and drops it once #1999 merges.
    edges = {
        (kind, str(target))
        for _sl, source, kind, _tl, target in store.edges
        if str(source).endswith(".Use.Run")
        and kind
        in (cs.RelationshipType.CALLS.value, cs.RelationshipType.INSTANTIATES.value)
    }
    suffixed = re.compile(r"\.Box@\d+$")
    twin = next(t for kind, t in edges if kind == "INSTANTIATES" and suffixed.search(t))
    assert ("CALLS", f"{twin}.Box(T)") in edges, sorted(edges)
    # The plain twin's constructor still gets its edge.
    assert ("CALLS", "proj.src.Zeta.Box.Zeta.Box.Box") in edges or (
        "CALLS",
        "proj.src.Zeta.Box.Box.Box",
    ) in edges, sorted(edges)
