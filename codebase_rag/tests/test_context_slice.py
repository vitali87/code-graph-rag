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

    # Positive equality, not `"\r" not in ...`: the weak form is satisfied by
    # any implementation that removes the CR by any means, including the
    # swapped ordering `.replace("\r", "\n").replace("\r\n", "\n")` -- which
    # doubles every line break and is exactly the mistake `_normalise` had to
    # avoid. Pin the whole text so a wrong fold is visible.
    assert one == "def scale(a: int) -> int:", repr(one)
    assert many == "def scale(a: int) -> int:\n    return a * 2", repr(many)
    # `_header` returns the definition's first line, identical under either
    # ending once the fold is correct. Pin it exactly, for the same reason as
    # the two above: a presence check passes for any CR removal at all.
    assert head == "def scale(a: int) -> int:", repr(head)


# The disk path above is the FALLBACK. `_target_piece` prefers
# `definition["source"]`, which `graph_query.definition` fills by calling
# `extract_source_lines` itself -- the same disk reader, but reached without
# going through `_lines`. Normalising only `_lines` therefore left the PREFERRED
# path broken, and CI kept failing on Windows while this suite was green.
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_graph_stored_source_is_normalised(newline: str) -> None:
    from codebase_rag.context_slice import _target_piece

    stored = newline.join(["def scale(a: int) -> int:", "    return a * 2", ""])
    piece = _target_piece(
        {  # type: ignore[arg-type]
            "qualified_name": "p.m.scale",
            "path": "m.py",
            "start_line": 1,
            "end_line": 2,
            "source": stored,
        },
        None,
    )

    assert piece.source == "def scale(a: int) -> int:\n    return a * 2", repr(
        piece.source
    )


# A lone CR (old-Mac endings, and what a truncated CRLF write leaves behind) is
# the only input that separates a correct fold from `.replace("\r", "")`. Both
# satisfy every CRLF assertion above, because on CRLF input they are identical;
# they differ only here, where dropping the CR joins two lines into one. Without
# this case the second `.replace` in `_normalise` is untested.
def test_a_lone_carriage_return_becomes_a_newline(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _lines

    (tmp_path / "mac.py").write_bytes(b"def scale(a: int) -> int:\r    return a * 2\r")

    assert (
        _lines(tmp_path, "mac.py", 1, 2)
        == "def scale(a: int) -> int:\n    return a * 2"
    )


# `_test_pieces` is the third `_normalise` call site and the only one no CRLF
# test reached: the fixture writes via `Path.write_text`, which emits LF, so
# reverting that one call left the suite green. Drive the contract it depends
# on -- `graph_query.definition` reading a CRLF file off disk -- so the piece
# built from it is pinned to the folded text.
def test_definition_source_from_a_crlf_file_is_normalised(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _normalise
    from codebase_rag.utils.source_extraction import extract_source_lines

    (tmp_path / "t.py").write_bytes(b"def test_run():\r\n    assert run() == 6\r\n")
    raw = extract_source_lines(tmp_path / "t.py", 1, 2)

    # The reader deliberately preserves the file's bytes; the fold is the
    # caller's job, which is precisely why every consumer needs `_normalise`.
    assert "\r" in (raw or ""), repr(raw)
    assert (
        _normalise(raw or "").rstrip("\n") == "def test_run():\n    assert run() == 6"
    ), repr(raw)


# The test above composes `extract_source_lines` and `_normalise` by hand, which
# proves the pair folds correctly but never enters `_test_pieces` -- so deleting
# that call site's `_normalise` left the file green. Drive the function itself,
# with the graph reads stubbed, so the third call site is actually pinned.
def test_test_pieces_normalises_the_source_it_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codebase_rag import context_slice as cs_mod
    from codebase_rag.utils.source_extraction import extract_source_lines

    (tmp_path / "test_app.py").write_bytes(
        b"def test_run():\r\n    assert run() == 6\r\n"
    )

    class _Reach:
        @staticmethod
        def build(_fetch: object, _project: str) -> _Reach:
            return _Reach()

        @staticmethod
        def tests_reaching(_qn: str) -> list[dict[str, object]]:
            return [
                {
                    "qualified_name": "p.test_app.test_run",
                    "path": "test_app.py",
                    "depth": 0,
                    "through": "p.app.run",
                }
            ]

    def _definition(
        _fetch: object, _project: str, _qn: str, _root: Path | None
    ) -> dict[str, object]:
        return {
            "qualified_name": "p.test_app.test_run",
            "path": "test_app.py",
            "start_line": 1,
            "end_line": 2,
            "source": extract_source_lines(tmp_path / "test_app.py", 1, 2),
        }

    monkeypatch.setattr(cs_mod, "ReachIndex", _Reach)
    monkeypatch.setattr(cs_mod.graph_query, "definition", _definition)

    pieces = cs_mod._test_pieces(lambda *a, **k: [], "p", "p.app.run", tmp_path)

    assert len(pieces) == 1, repr(pieces)
    assert pieces[0].source == "def test_run():\n    assert run() == 6", repr(
        pieces[0].source
    )


# The indexer writes absolute_path through `cached_resolve_posix`, so the lookup
# key must be POSIX too. Building it with `str(...resolve())` matched on Linux and
# macOS and emitted backslashes on Windows, so the doc query found nothing there,
# no CONTEXT_WHY_DOC candidate was built, and the grouping raised KeyError.
#
# This bug CANNOT be caught behaviourally on POSIX. `WindowsPath` cannot be
# instantiated here, and for any `PosixPath` `str(p.resolve())` and
# `p.resolve().as_posix()` are equal by construction -- so no input makes the two
# implementations disagree, and any behavioural test is satisfied by both. Two
# attempts at one (comparing the key against the helper; patching the helper the
# old code never calls) both passed against the reverted implementation.
#
# So assert the SOURCE instead: `_doc_pieces` must build its key with the same
# helper the writer uses. A static check is weaker than a behavioural one, but it
# is the only form that can fail on the machines this suite actually runs on, and
# it fails immediately if someone reintroduces a hand-built key.
def test_doc_lookup_key_is_built_with_the_writers_helper() -> None:
    import inspect

    from codebase_rag import context_slice as cs_mod

    source = inspect.getsource(cs_mod._doc_pieces)

    # Match on the two facts rather than on an exact line, so a behaviour-
    # preserving refactor (renaming the local, inlining the call) does not fail
    # this. Both halves are needed: the first alone passes if a stray
    # `str(...resolve())` is reintroduced alongside the helper, the second alone
    # passes if the key is built some third way that is also wrong on Windows.
    assert "cached_resolve_posix(repo_root / path)" in source, (
        "_doc_pieces must build its lookup key with cached_resolve_posix, the "
        "helper the indexer writes absolute_path with"
    )
    assert "str((repo_root / path).resolve())" not in source, (
        "_doc_pieces must not hand-build the lookup key: str(...resolve()) is "
        "backslash-separated on Windows and matches no stored absolute_path"
    )


# A broken skip guard is invisible: if `_markdown_unavailable` silently returned
# a constant, the two tests it gates would SKIP or would run without the grammar,
# and a skip reads as green in every summary while also being the legitimate
# state on a base install. So there is no baseline that looks wrong.
#
# Reading whatever THIS machine has cannot catch it. Both grammars load here, so
# `not (block and inline)` is False and a guard hard-wired to `return False`
# agrees on every full install -- and that is the worse mutant, because it means
# "never skip", so a base install runs the doc tests without the grammar and
# they FAIL rather than skip, which is #1591 in its original form. Only the
# stuck-True twin disagrees on this machine, so testing one mutant makes the
# assertion look sound.
#
# Drive all four loader states instead. No environment assumption, both stuck
# mutants fail at least one row, and the `and` is pinned: a guard using `or`
# passes the both-load row and differs on the mixed ones.
@pytest.mark.parametrize(
    ("block_ok", "inline_ok"),
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_the_markdown_skip_guard_tracks_the_real_loaders(
    monkeypatch: pytest.MonkeyPatch, block_ok: bool, inline_ok: bool
) -> None:
    from codebase_rag.parsers import document_tier

    monkeypatch.setattr(
        document_tier, "_load_parser", lambda: object() if block_ok else None
    )
    monkeypatch.setattr(
        document_tier, "_load_inline_parser", lambda: object() if inline_ok else None
    )

    assert _markdown_unavailable() == (not (block_ok and inline_ok)), (
        f"guard says unavailable={_markdown_unavailable()} with "
        f"block={block_ok} inline={inline_ok}"
    )


# `graph_query.definition` returns a row with `start_line=None` when the symbol
# is not in the graph (graph_query.py:213). `_target_piece` defaulted that to 1
# while its sibling `_test_pieces` defaulted to 0, so the same missing value
# produced an empty excerpt in one place and LINE 1 OF THE FILE in the other --
# an unrelated import presented as the definition's body, with no error. The
# sentinel one field up from the one the fix was about, unpinned because the
# fix was about the other one.
def test_a_target_missing_from_the_graph_yields_no_excerpt(tmp_path: Path) -> None:
    from codebase_rag.context_slice import _target_piece

    (tmp_path / "m.py").write_text("import os\ndef real():\n    return 1\n")

    piece = _target_piece(
        {  # type: ignore[arg-type]
            "label": None,
            "qualified_name": "p.m.missing",
            "path": "m.py",
            "start_line": None,
            "end_line": None,
            "name": None,
            "docstring": None,
            "source": None,
            "found": False,
        },
        tmp_path,
    )

    assert piece.source == "", repr(piece.source)
    assert "import os" not in piece.source, repr(piece.source)
