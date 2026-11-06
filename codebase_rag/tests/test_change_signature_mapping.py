# change_signature (issue #1533): how the mapping sources each new parameter
# at a call site, and the sites it cannot complete, which are listed.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature import change_signature
from codebase_rag.editing.signature_spec import SignatureRefused, parse_mapping
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.change_signature_support import (
    FIXTURE,
    HELPER,
    PROJECT,
    _index,
    _project,
    _read,
    _smoke,
    indexed_fixture,
)
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    return indexed_fixture(temp_repo)


# --- the mapping -------------------------------------------------------------------


def test_a_parameter_renamed_by_index_renames_keyword_sites(temp_repo: Path) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a: int, b: str) -> str:\n    return b * a\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(2, 'x')\n\n\n"
                "def run_kw():\n    return helper(b='y', a=2)\n"
            ),
        },
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["times: int", "text: str"],
        {"times": "0", "text": "1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    util = _read(root, "pkg/util.py")
    assert "def helper(times: int, text: str) -> str:" in util
    # The body follows the rename, or the definition would be broken.
    assert "    return text * times\n" in util
    app = _read(root, "pkg/app.py")
    assert "return helper(2, 'x')" in app
    # Keyword values keep their order and form; only the names follow.
    assert "return helper(text='y', times=2)" in app
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(
        root,
        "from pkg.app import run, run_kw\nassert (run(), run_kw()) == ('xx', 'yy')",
    )


def test_a_value_after_an_omitted_default_is_spelled_by_keyword(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "b", "n: int = 0"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    app = _read(root, "pkg/app.py")
    # `helper(2)` relied on b's default; the value for n cannot sit in b's
    # slot, so it goes by keyword.
    assert "return helper(2, n=1)" in app
    assert "return helper(2, b='y', n=1)" in app
    assert "return helper(3, 'z', 1)" in app
    _smoke(
        root,
        "from pkg.app import run, run_kw, run_both\n"
        "assert (run(), run_kw(), run_both()) == ('xx', 'yy', 'zzz')\n",
    )


def test_an_unmapped_required_parameter_leaves_every_site_and_lists_it(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "def helper(a: int, n: int, b: str = 'x') -> str:" in _read(
        root, "pkg/util.py"
    )
    assert _read(root, "pkg/app.py") == FIXTURE["pkg/app.py"]
    assert sorted((u.path, u.line) for u in report.unmapped) == [
        ("pkg/app.py", 5),
        ("pkg/app.py", 9),
        ("pkg/app.py", 13),
    ]
    assert all("`n`" in u.reason for u in report.unmapped)
    # Listed sites satisfy the contract: the operation said it left them.
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message


def _strip_location(store: _StatefulIngestor, caller: str) -> None:
    edge = next(
        e
        for e in store.edge_props
        if e[1] == caller and e[2] == cs.RelationshipType.CALLS.value and e[4] == HELPER
    )
    props = store.edge_props[edge]
    props[cs.KEY_RESOLUTION] = cs.EdgeResolution.DYNAMIC.value
    for key in (cs.KEY_LINE, cs.KEY_COL, cs.KEY_END_LINE, cs.KEY_END_COL):
        props.pop(key, None)


def test_a_site_whose_recorded_end_matches_no_call_is_listed(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # A stale end position must not fall back to another call sharing the
    # start: the site is listed rather than the wrong call rewritten.
    root, store, _updater = repo
    edge = next(
        e
        for e in store.edge_props
        if e[1] == f"{PROJECT}.pkg.app.run_kw"
        and e[2] == cs.RelationshipType.CALLS.value
        and e[4] == HELPER
    )
    store.edge_props[edge][cs.KEY_END_COL] = 99
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    (skipped,) = report.unmapped
    assert skipped.owner == f"{PROJECT}.pkg.app.run_kw"
    assert "no call" in skipped.reason
    assert "helper(2, b='y')" not in report.diff
    assert "+    return helper(3, 1, 'z')" in report.diff


def test_chained_calls_sharing_a_start_are_both_rewritten(temp_repo: Path) -> None:
    # `B().area(2).area(3)`: both calls start where `B` does and differ only
    # in where they end. The graph records both; neither may be dropped.
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/shapes.py": (
                "class B:\n"
                "    def area(self, scale):\n"
                "        return self\n\n\n"
                "def run():\n"
                "    return B().area(2).area(3)\n"
            ),
        },
    )
    store, updater = _index(root)
    run_qn = f"{PROJECT}.pkg.shapes.run"
    stored = store.fetch_all

    def both_links(query: str, params: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
        out = []
        for row in stored(query, params):
            if row.get(cs.KEY_QUALIFIED_NAME) != run_qn:
                out.append(row)
                continue
            for end_col in (22, 30):
                link = dict(row)
                link[cs.KEY_LINE] = 7
                link[cs.KEY_COL] = 11
                link[cs.KEY_END_LINE] = 7
                link[cs.KEY_END_COL] = end_col
                out.append(link)
        return out

    report = change_signature(
        root,
        both_links,
        PROJECT,
        f"{PROJECT}.pkg.shapes.B.area",
        ["scale", "unit: str"],
        {"unit": "='m'"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    assert sum(1 for s in report.sites if s.kind == "call") == 2
    assert "return B().area(2, 'm').area(3, 'm')" in _read(root, "pkg/shapes.py")
    _smoke(root, "from pkg.shapes import run, B\nassert isinstance(run(), B)")


def test_every_site_without_a_location_is_listed(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    _strip_location(store, f"{PROJECT}.pkg.app.run")
    _strip_location(store, f"{PROJECT}.pkg.app.run_kw")
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    assert sorted(u.owner for u in report.unmapped) == [
        f"{PROJECT}.pkg.app.run",
        f"{PROJECT}.pkg.app.run_kw",
    ]
    assert all("no location" in u.reason for u in report.unmapped)
    assert "+    return helper(3, 1, 'z')" in report.diff


def test_a_site_passing_surplus_arguments_is_listed_not_truncated(
    temp_repo: Path,
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a, b):\n    return a + b\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(1, 2)\n\n\n"
                "def broken():\n    return helper(1, 2, 3)\n"
            ),
        },
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["b", "a"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    app = _read(root, "pkg/app.py")
    assert "return helper(2, 1)" in app
    assert "return helper(1, 2, 3)" in app
    (skipped,) = report.unmapped
    assert (skipped.path, skipped.line) == ("pkg/app.py", 9)
    assert "3" in skipped.reason
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message


@pytest.mark.parametrize(
    ("site", "fragment"),
    [
        ("helper(*args)", "*args"),
        ("helper(2, **opts)", "**opts"),
        ("helper(2, c=1)", "c"),
    ],
)
def test_a_site_the_mapping_cannot_read_is_listed(
    temp_repo: Path, site: str, fragment: str
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": FIXTURE["pkg/util.py"],
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                f"def run(args=(), opts={{}}):\n    return {site}\n"
            ),
        },
    )
    store, _updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    (skipped,) = report.unmapped
    assert (skipped.path, skipped.line) == ("pkg/app.py", 5)
    assert fragment in skipped.reason
    assert site not in report.diff


@pytest.mark.parametrize(
    ("mapping", "reason"),
    [
        ({"n": "c"}, r"neither an old parameter"),  # no old parameter c
        ({"n": "2"}, r"only 2 old parameter"),  # only two old parameters
        ({"m": "=1"}, r"not a new parameter"),  # no new parameter m
        # One old value cannot feed two parameters. Matched by reason: with
        # the duplicate check removed this case was still refused, by the
        # body check seeing `a` already in use, so a bare `raises` could
        # not tell the two apart.
        ({"a": "b", "b": "b"}, r"cannot feed both"),
    ],
)
def test_a_mapping_that_names_nothing_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
    mapping: dict[str, str],
    reason: str,
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match=reason):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", "n: int", "b"],
            mapping,
            dry_run=True,
        )
    assert _read(root, "pkg/util.py") == FIXTURE["pkg/util.py"]


def test_parse_mapping_text_forms() -> None:
    assert parse_mapping(["n==1", "a=0", "b=old_b", "s=='x'"]) == {
        "n": "=1",
        "a": "0",
        "b": "old_b",
        "s": "='x'",
    }
    with pytest.raises(SignatureRefused):
        parse_mapping(["n"])
