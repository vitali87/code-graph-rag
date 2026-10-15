"""A constructor under a duplicate-suffixed class (`Box`1@12.Box(T)`) is that
class's constructor (issue #2007).

The generic twin of a same-file `Box` is its own type, `Box`1` (issue
#2579), so `new Box<int>(1)` runs its constructor and not the plain twin's.
A duplicate marker is now left to a second part of one partial type.
"""

from __future__ import annotations

import re
from pathlib import Path

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

USE = (
    "using Zeta;\n\nnamespace App;\n\npublic class Use\n{\n"
    "    public void Run() { var b = new Box<int>(1); }\n}\n"
)
PLAIN = "public class Box\n{\n    public Box() { }\n}\n\n"


def _edges(tmp_path: Path, box_source: str) -> set[tuple[str, str]]:
    root = tmp_path / "proj"
    files = {"src/Zeta/Box.cs": box_source, "src/App/Use.cs": USE}
    for rel, source in files.items():
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
    return {
        (kind, str(target))
        for _sl, source, kind, _tl, target in store.edges
        if str(source).endswith(".Use.Run")
        and kind
        in (cs.RelationshipType.CALLS.value, cs.RelationshipType.INSTANTIATES.value)
    }


def test_the_generic_twins_constructor_takes_a_calls_edge(tmp_path: Path) -> None:
    edges = _edges(
        tmp_path,
        "namespace Zeta;\n\n"
        + PLAIN
        + "public class Box<T>\n{\n    public Box(T value) { }\n}\n",
    )
    twin = "proj.src.Zeta.Box.Box`1"
    assert ("INSTANTIATES", twin) in edges, sorted(edges)
    assert ("CALLS", f"{twin}.Box(T)") in edges, sorted(edges)
    # The plain twin is a different type: `new Box<int>` does not build it.
    assert ("CALLS", "proj.src.Zeta.Box.Box.Box") not in edges, sorted(edges)
    assert ("INSTANTIATES", "proj.src.Zeta.Box.Box") not in edges, sorted(edges)


def test_a_constructor_on_a_second_partial_part_takes_a_calls_edge(
    tmp_path: Path,
) -> None:
    edges = _edges(
        tmp_path,
        "namespace Zeta;\n\n"
        + PLAIN
        + "public partial class Box<T> { }\n\n"
        + "public partial class Box<T>\n{\n    public Box(T value) { }\n}\n",
    )
    suffixed = re.compile(r"\.Box`1@\d+\.Box\(T\)$")
    assert any(kind == "CALLS" and suffixed.search(target) for kind, target in edges), (
        sorted(edges)
    )
