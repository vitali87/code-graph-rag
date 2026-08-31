# context(target, budget_tokens) (issue #1536): a graph-ranked slice within a
# token budget. The graph is the in-memory stateful ingestor over a real
# index of a fixture with a target, callers, callees, a returned type, a
# reaching test, an unrelated module and a document linking to the file.

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.context_slice import context
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.utils.token_utils import count_tokens
from evals.cgr_graph import _StatefulIngestor


# The doc-section reasons come from DocumentTier, which builds its own markdown
# parser and degrades to plain File nodes when the grammar is absent
# (`document_tier._load_parser` returns None, `handles()` then False). The
# grammar ships in the `treesitter-full` extra, so on a base install these two
# tests would raise KeyError/ValueError on CONTEXT_WHY_DOC rather than skip --
# which ci.yml's base-install job exists to prevent (issues #1371, #1410).
# Scoped to the two tests that assert on a doc reason: the other three must
# keep running there, so a module-level importorskip would hide them.
#
# Ask the production loaders rather than testing importability: `find_spec` only
# proves the module is FINDABLE, while `_load_parser` also fails when
# `Language(...)` cannot be constructed from an installed-but-unusable wheel. On
# Windows the import succeeded and the construction did not, so the skip never
# fired and the test raised ValueError on a missing doc piece instead. Both
# loaders are needed: the block grammar builds the sections and the inline one
# builds the link edge that puts a section in the slice.
def _markdown_unavailable() -> bool:
    from codebase_rag.parsers import document_tier

    return document_tier._load_parser() is None or (
        document_tier._load_inline_parser() is None
    )


_needs_markdown = pytest.mark.skipif(
    _markdown_unavailable(),
    reason="markdown grammar unusable (treesitter-full extra)",
)

PROJECT = "context_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/models.py": "class Total:\n    def __init__(self, value: int):\n        self.value = value\n",
    "pkg/util.py": (
        "from pkg.models import Total\n\n\n"
        "def scale(a: int) -> int:\n    return a * 2\n\n\n"
        "def helper(items: list[int]) -> Total:\n"
        '    """Sum the scaled items."""\n'
        "    result = 0\n"
        "    for item in items:\n"
        "        result += scale(item)\n"
        "    return Total(result)\n"
    ),
    "pkg/app.py": (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper([1, 2]).value\n\n\n"
        "def run_twice():\n    first = helper([3])\n    return first.value * 2\n\n\n"
        "def run_again():\n    return helper([4]).value\n"
    ),
    "pkg/unrelated.py": "def alone():\n    return 'nothing to do with helper'\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 6\n"
    ),
    "docs/guide.md": (
        "# Guide\n\nIntroduction.\n\n## Summing\n\n"
        "`helper` sums the scaled items; see [util](../pkg/util.py).\n\n"
        "## Other\n\nNothing here.\n"
    ),
}


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


def _by_why(slice_: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for piece in slice_["pieces"]:
        out.setdefault(piece["why_included"], []).append(piece)
    return out


@_needs_markdown
def test_four_thousand_token_budget_returns_the_neighbourhood_and_nothing_else(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    slice_ = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), 4000, root)
    assert slice_["resolved"] == _qn("pkg.util.helper")
    grouped = _by_why(slice_)
    target = grouped[cs.CONTEXT_WHY_TARGET]
    assert len(target) == 1 and target[0]["source"].startswith(
        "def helper(items: list[int]) -> Total:"
    )
    assert target[0]["file"] == "pkg/util.py" and target[0]["span"] == [8, 13]
    callers = grouped[cs.CONTEXT_WHY_CALLER]
    assert {(c["qualified_name"], c["source"]) for c in callers} == {
        (_qn("pkg.app.run"), "return helper([1, 2]).value"),
        (_qn("pkg.app.run_twice"), "first = helper([3])"),
        (_qn("pkg.app.run_again"), "return helper([4]).value"),
    }
    callees = grouped[cs.CONTEXT_WHY_CALLEE]
    assert {(c["qualified_name"], c["source"]) for c in callees} >= {
        (_qn("pkg.util.scale"), "def scale(a: int) -> int:"),
    }
    assert [t["qualified_name"] for t in grouped[cs.CONTEXT_WHY_RETURNS]] == [
        _qn("pkg.models.Total")
    ]
    tests = grouped[cs.CONTEXT_WHY_TEST.format(depth=2, through=_qn("pkg.app.run"))]
    assert [t["qualified_name"] for t in tests] == [_qn("tests.test_app.test_run")]
    assert tests[0]["source"] == "def test_run():\n    assert run() == 6"
    docs = grouped[cs.CONTEXT_WHY_DOC]
    assert len(docs) == 1 and docs[0]["file"] == "docs/guide.md"
    assert "`helper` sums the scaled items" in docs[0]["source"]
    # Nothing from the unrelated module, in any role.
    assert all("unrelated" not in p["qualified_name"] for p in slice_["pieces"])
    assert slice_["omitted"] == [] and not slice_["truncated"]
    assert slice_["used_tokens"] == sum(p["tokens"] for p in slice_["pieces"])
    assert slice_["used_tokens"] <= 4000


def test_token_count_never_exceeds_the_budget(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    full = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), 4000, root)
    for budget in (full["used_tokens"] - 1, 60, 25, 5):
        slice_ = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), budget, root)
        assert slice_["used_tokens"] <= budget
        assert sum(count_tokens(p["source"]) for p in slice_["pieces"]) <= budget
        assert slice_["omitted"] or slice_["truncated"]
    shrunk = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), 25, root)
    # The target keeps as many of its lines as fit and comes first.
    assert shrunk["truncated"]
    assert shrunk["pieces"][0]["why_included"] == cs.CONTEXT_WHY_TARGET
    assert shrunk["pieces"][0]["source"].startswith("def helper(")
    tiny = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), 5, root)
    # Too small for even the first line: the target is named as omitted
    # rather than padded with nothing, and the budget still holds.
    assert tiny["truncated"] and tiny["used_tokens"] <= 5
    assert tiny["omitted"][0].startswith(_qn("pkg.util.helper"))


@_needs_markdown
def test_ranking_prefers_distance_then_trace_hotness(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    edge = next(
        e
        for e in store.edges
        if e[1] == _qn("pkg.app.run_twice")
        and e[2] == "CALLS"
        and e[4] == _qn("pkg.util.helper")
    )
    store.edge_props.setdefault(edge, {})[cs.TRACE_PROP_CALL_COUNT] = 40
    slice_ = context(store.fetch_all, PROJECT, _qn("pkg.util.helper"), 4000, root)
    order = [(p["why_included"], p["qualified_name"]) for p in slice_["pieces"]]
    assert order[0] == (cs.CONTEXT_WHY_TARGET, _qn("pkg.util.helper"))
    first_callers = [q for why, q in order if why == cs.CONTEXT_WHY_CALLER]
    assert first_callers[0] == _qn("pkg.app.run_twice")
    distances = [p["why_included"] for p in slice_["pieces"]]
    assert distances.index(cs.CONTEXT_WHY_DOC) > distances.index(cs.CONTEXT_WHY_CALLER)


def test_targets_by_bare_name_location_and_free_text(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    assert context(store.fetch_all, PROJECT, "helper", 500, root)["resolved"] == _qn(
        "pkg.util.helper"
    )
    assert context(store.fetch_all, PROJECT, "pkg/util.py:11", 500, root)[
        "resolved"
    ] == _qn("pkg.util.helper")
    missing = context(store.fetch_all, PROJECT, "sum the scaled items", 500, root)
    assert missing["resolved"] is None and missing["pieces"] == []

    def search(text: str) -> list:
        return [
            {
                "node_id": 1,
                "qualified_name": _qn("pkg.util.helper"),
                "name": "helper",
                "type": "Function",
                "score": 0.9,
            },
            {
                "node_id": 2,
                "qualified_name": _qn("pkg.app.run_twice"),
                "name": "run_twice",
                "type": "Function",
                "score": 0.7,
            },
        ]

    found = context(
        store.fetch_all, PROJECT, "sum the scaled items", 4000, root, search=search
    )
    assert found["resolved"] == _qn("pkg.util.helper")
    callers = [
        p["qualified_name"]
        for p in found["pieces"]
        if p["why_included"] == cs.CONTEXT_WHY_CALLER
    ]
    # Similarity breaks the tie among callers: run_twice scored, run did not.
    assert callers[0] == _qn("pkg.app.run_twice")


async def test_mcp_context_tool(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, _updater = repo
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    schema = next(
        s for s in registry.get_tool_schemas() if s.name == cs.MCPToolName.CONTEXT
    )
    assert schema.inputSchema["required"] == [cs.MCPParamName.TARGET]
    payload = await registry.context(
        target=_qn("pkg.util.helper"), budget_tokens=300, project=PROJECT
    )
    assert isinstance(payload, dict)
    assert (
        payload["resolved"] == _qn("pkg.util.helper") and payload["used_tokens"] <= 300
    )


# Excerpts are read from disk and trimmed with .strip("\n") / .rstrip("\n"),
# which leaves the b"\r" of a CRLF file attached to every line. The slice then
# reported `def scale(a: int) -> int:\r`, so every Windows unit job failed here
# while Linux and macOS stayed green -- no fixture in this suite used CRLF.
# Drive the helpers directly on both endings; the LF case is the control that
# proves the assertion is not vacuous.
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_excerpts_carry_no_carriage_return(tmp_path: Path, newline: str) -> None:
    from codebase_rag.context_slice import _header, _line, _lines

    body = newline.join(["def scale(a: int) -> int:", "    return a * 2", ""])
    (tmp_path / "mod.py").write_bytes(body.encode(cs.ENCODING_UTF8))

    one = _line(tmp_path, "mod.py", 1)
    many = _lines(tmp_path, "mod.py", 1, 2)
    head = _header(tmp_path, "mod.py", 1, 2)

    assert one == "def scale(a: int) -> int:", repr(one)
    assert "\r" not in many, repr(many)
    assert "\r" not in head, repr(head)
