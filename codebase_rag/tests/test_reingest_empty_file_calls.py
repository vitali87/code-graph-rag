"""A file that parses to no definitions must retract its old CALLS edges (#1794).

`reingest` deletes a re-parsed file's Module subtree and then re-resolves its
calls. The re-resolution reads `_func_class_captures_cache`, which was only
WRITTEN when the new parse produced captures -- so a file emptied between runs
skipped the write and left the PREVIOUS parse's captures in place. Those
captures hold nodes from a tree that has since been discarded, so the call
walk re-emitted an edge out of a function no longer present in the source.

The graph then asserted `proj.pkg.app.run CALLS proj.pkg.util.helper` with
`run` deleted -- an edge whose source node does not exist. A clean index of
the same tree emits nothing, so the incremental and batch paths disagreed.

This is the watcher's normal mid-write state: an editor that truncates before
writing leaves a zero-length file, and a save caught at that instant produced
exactly this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

UTIL = "def helper():\n    return 1\n"
APP = "from pkg.util import helper\n\n\ndef run():\n    return helper()\n"


def _fixture(root: Path) -> None:
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "util.py").write_text(UTIL, encoding="utf-8")
    (root / "pkg" / "app.py").write_text(APP, encoding="utf-8")


def _calls(store: _StatefulIngestor) -> set[tuple[str, str]]:
    return {
        (str(src), str(dst))
        for (_sl, src, rel, _tl, dst) in store.edges
        if rel == "CALLS"
    }


def _built(root: Path) -> tuple[GraphUpdater, _StatefulIngestor]:
    parsers, queries = load_parsers()
    if "python" not in {str(k) for k in parsers}:
        pytest.skip("python parser not available")
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="proj",
    )
    updater.run(force=True)
    store.flush_all()
    return updater, store


@pytest.mark.parametrize(
    ("new_content", "label"),
    [
        ("", "emptied"),
        ("\x00\xff not python at all \x00", "unparseable"),
    ],
    ids=["emptied", "unparseable"],
)
def test_a_file_parsing_to_no_definitions_retracts_its_calls(
    tmp_path: Path, new_content: str, label: str
) -> None:
    """Both routes to "no definitions" must retract, not just the empty one.

    Parametrised because the two reach the same state by different paths: an
    empty file produces no captures at all, while unparseable bytes produce a
    tree whose captures are empty. A fix guarding only on `content == ""`
    would pass the first and fail the second.
    """
    _fixture(tmp_path)
    updater, store = _built(tmp_path)

    assert _calls(store) == {("proj.pkg.app.run", "proj.pkg.util.helper")}, (
        "fixture guard: the initial index must emit the edge that is later "
        "expected to be retracted, or this test proves nothing"
    )

    (tmp_path / "pkg" / "app.py").write_text(new_content, encoding="utf-8")
    updater.reingest(["pkg/app.py"])
    store.flush_all()

    assert _calls(store) == set(), (
        f"a {label} file left a CALLS edge out of a function that no longer "
        f"exists in the source: {sorted(_calls(store))}"
    )


def test_the_incremental_result_matches_a_clean_index(tmp_path: Path) -> None:
    """The property that actually matters, stated against a real rebuild.

    Asserting an empty set is right here but would not notice a fix that
    over-retracted. Comparing against a clean index of the same tree pins
    incremental and batch together whatever the correct answer turns out to
    be, which is the invariant the issue is really about.
    """
    _fixture(tmp_path)
    updater, store = _built(tmp_path)
    (tmp_path / "pkg" / "app.py").write_text("", encoding="utf-8")
    updater.reingest(["pkg/app.py"])
    store.flush_all()

    clean_root = tmp_path.parent / "clean"
    clean_root.mkdir()
    _fixture(clean_root)
    (clean_root / "pkg" / "app.py").write_text("", encoding="utf-8")
    _, clean_store = _built(clean_root)

    assert _calls(store) == _calls(clean_store), (
        "the incremental path and a clean index disagree about CALLS: "
        f"incremental={sorted(_calls(store))} clean={sorted(_calls(clean_store))}"
    )


def test_a_still_defined_function_keeps_its_calls(tmp_path: Path) -> None:
    """The control against over-retracting.

    The obvious wrong fix is to drop a re-parsed file's captures and never
    rewrite them, which retracts every edge rather than only the stale ones.
    This edit leaves `run` defined and still calling `helper`, so the edge
    must SURVIVE -- and the issue records that this shape was already handled
    correctly, so it must stay that way.
    """
    _fixture(tmp_path)
    updater, store = _built(tmp_path)

    (tmp_path / "pkg" / "app.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper() + 1\n",
        encoding="utf-8",
    )
    updater.reingest(["pkg/app.py"])
    store.flush_all()

    assert _calls(store) == {("proj.pkg.app.run", "proj.pkg.util.helper")}, (
        "a still-present call was retracted, so the fix over-corrected and "
        "drops live edges on every re-parse"
    )


def test_a_module_level_reference_survives_the_re_parse(tmp_path: Path) -> None:
    """The other half of the fix, which nothing here was pinning.

    Writing empty cache entries made them possible for the first time, and a
    pre-existing perf skip read "no call captures and no function captures" as
    "nothing to do". That is true only when the parse produced SOMETHING: a
    module-level reference -- a dispatch dict holding an imported handler --
    is a real call edge that lives under NEITHER capture, so an empty entry
    made the walk skip a file that still had an edge to emit.

    Reverting that guard leaves every other test in this file green; the only
    thing catching it was `test_src_layout_dispatch_dict_value` in
    `test_python_source_root_imports.py`, whose name gives no hint that it
    guards this behaviour, so rewriting that test would silently un-fix this
    branch (greptile-local, #1794).
    """
    _fixture(tmp_path)
    (tmp_path / "pkg" / "dispatch.py").write_text(
        "from pkg.util import helper\n\nTABLE = {'h': helper}\n", encoding="utf-8"
    )
    updater, store = _built(tmp_path)

    edge = ("proj.pkg.dispatch", "proj.pkg.util.helper")
    assert edge in _calls(store), (
        "fixture guard: the module-level reference must be emitted by the "
        f"initial index, or the re-parse below proves nothing: {sorted(_calls(store))}"
    )

    # Re-parse the dispatch module itself. Its captures are empty -- the
    # reference is neither a call site nor a function -- so this is exactly
    # the file the skip would drop.
    updater.reingest(["pkg/dispatch.py"])
    store.flush_all()

    assert edge in _calls(store), (
        "a module-level reference was dropped by the re-parse: the file's "
        f"captures are empty, so the call walk skipped it entirely: {sorted(_calls(store))}"
    )


def test_an_unavailable_query_is_not_cached_as_an_empty_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Nobody looked" must not be recorded as "there is nothing there".

    `combined_captures` stays None when a language has no combined query, or
    when building it raised. Writing `{}` for that case told the call walk the
    file has no functions, and it then attributed a call to the MODULE
    alongside the correct function-owned edge -- a spurious
    `proj.pkg.caller CALLS ...` beside `proj.pkg.caller.run CALLS ...`.

    The same absent-vs-empty distinction the fix above depends on, in the
    opposite direction: there, absent wrongly meant "nothing changed"; here,
    empty wrongly means "nothing is there". Both are cases of a cache entry
    that cannot say which question it is answering (Greptile, PR #1833).

    Asserted against the edge set a working combined query produces, so the
    test states the invariant -- an unavailable query must not CHANGE the
    answer -- rather than a hardcoded expectation.
    """
    from codebase_rag import constants as cs
    from codebase_rag.parsers import definition_processor as dp

    _fixture(tmp_path)
    (tmp_path / "pkg" / "caller.py").write_text(
        "from pkg.util import helper\n\n\ndef run():\n    return helper()\n",
        encoding="utf-8",
    )
    _, with_query = _built(tmp_path)
    expected = _calls(with_query)
    assert expected, (
        "fixture guard: the working-query index must emit at least one call "
        "edge, or the comparison below is vacuous"
    )

    monkeypatch.setitem(
        dp.COMBINED_FUNC_CLASS_IMPORT_QUERIES, cs.SupportedLanguage.PYTHON, None
    )
    _, without_query = _built(tmp_path)

    assert _calls(without_query) == expected, (
        "an unavailable combined query changed the call edges: "
        f"without={sorted(_calls(without_query))} with={sorted(expected)}"
    )
