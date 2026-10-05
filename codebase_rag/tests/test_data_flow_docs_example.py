"""The data-flow page's worked example lists the edges its snippet produces.

`docs/architecture/data-flow-edges.md` said its example body produces three
`FLOWS_TO` edges. Indexing it with the `io` capture produces four: `t` carries
`ENV::T` into `forward`, which prints it, so `ENV::T -> STDOUT` exists beside
`ENV::K -> STDOUT`, and the page's first example query returns two rows, not
the one its text implied (issue #2882). The test reads the snippet and the
edge diagram from the page itself, so the two cannot drift apart again.
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from unittest.mock import MagicMock

from codebase_rag import constants as cs
from codebase_rag.capture import resolve_capture
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers

_PAGE = (
    Path(__file__).resolve().parents[2] / "docs" / "architecture" / "data-flow-edges.md"
)
_SNIPPET = re.compile(r"```python\n(def build\(\):.*?)```", re.DOTALL)
_DIAGRAM = re.compile(r'<div class="cgr-flow">(.*?)<div class="legend">', re.DOTALL)
_EDGE = re.compile(
    r'<span class="node (res|code)">(.*?)</span>\s*'
    r'<span class="arrow flows"><span class="rel">FLOWS_TO · (\w+)(?: · ([\w:]+))?</span></span>\s*'
    r'<span class="node (res|code)">(.*?)</span>'
)
_RESOURCE_PREFIX = "resource::"
_PROJECT = "flowproj"
# The FLOWS_TO channel property (a MERGE key, see MERGE_KEY_PROPS_BY_REL).
_VIA = "via"

type _Edge = tuple[str, str, str, str | None]


def _page() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _node(kind: str, name: str) -> str:
    name = html.unescape(name)
    return (
        _RESOURCE_PREFIX + name
        if kind == "res"
        else f"{_PROJECT}{cs.SEPARATOR_DOT}{name}"
    )


def _documented_edges() -> set[_Edge]:
    diagram = _DIAGRAM.search(_page())
    assert diagram is not None, "the example's edge diagram is missing"
    return {
        (_node(src_kind, src), _node(dst_kind, dst), kind, via or None)
        for src_kind, src, kind, via, dst_kind, dst in _EDGE.findall(diagram.group(1))
    }


def _produced_edges(tmp_path: Path) -> set[_Edge]:
    snippet = _SNIPPET.search(_page())
    assert snippet is not None, "the example snippet is missing"
    repo = tmp_path / _PROJECT
    repo.mkdir()
    (repo / "flow.py").write_text(
        "import os\n\n\n" + snippet.group(1), encoding="utf-8"
    )
    parsers, queries = load_parsers()
    mock = MagicMock()
    GraphUpdater(
        ingestor=mock,
        repo_path=repo,
        parsers=parsers,
        queries=queries,
        capture=resolve_capture([cs.CaptureGroup.IO.value]),
    ).run()
    edges: set[_Edge] = set()
    for call in mock.ensure_relationship_batch.call_args_list:
        if str(call.args[1]) != cs.RelationshipType.FLOWS_TO:
            continue
        props = call.args[3] if len(call.args) > 3 else call.kwargs.get("properties")
        props = props or {}
        via = props.get(_VIA)
        edges.add(
            (
                str(call.args[0][2]),
                str(call.args[2][2]),
                str(props.get(cs.KEY_KIND)),
                str(via) if via is not None else None,
            )
        )
    return edges


def test_the_diagram_lists_every_edge_the_snippet_produces(tmp_path: Path) -> None:
    produced = _produced_edges(tmp_path)
    assert _documented_edges() == produced, sorted(produced)


def test_the_page_counts_the_edges_it_draws() -> None:
    count = len(_documented_edges())
    words = {3: "three", 4: "four", 5: "five"}
    assert f"The {words[count]} `FLOWS_TO` edges that body produces" in _page()


def test_the_env_to_stdout_query_names_both_rows() -> None:
    # Example query 1 returns one row per resource edge into STDOUT.
    page = _page()
    query_section = page[page.index("## Example queries") :]
    assert "ENV::K" in query_section and "ENV::T" in query_section, query_section[:800]
