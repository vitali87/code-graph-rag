# Edit algebra op 2, change_signature (issue #1533): the definition (and its
# override hierarchy) gets the new parameter list, and every graph-known call
# site is rewritten per an explicit mapping from new parameter to old value.
# Sites the mapping cannot complete, and sites the graph resolved by
# guesswork, are left as written and listed as unmapped. The graph is the
# in-memory stateful ingestor over a real index of a fixture repo.
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature import (
    SignatureRefused,
    change_signature,
    parse_mapping,
)
from codebase_rag.editing.transaction import load_history
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from evals.cgr_graph import _StatefulIngestor

PROJECT = "signature_fixture"
FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a: int, b: str = 'x') -> str:\n    return b * a\n",
    "pkg/app.py": (
        "from pkg.util import helper\n\n\n"
        "def run():\n    return helper(2)\n\n\n"
        "def run_kw():\n    return helper(2, b='y')\n\n\n"
        "def run_both():\n    return helper(3, 'z')\n"
    ),
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 'xx'\n"
    ),
}
HELPER = f"{PROJECT}.pkg.util.helper"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _index(root: Path) -> tuple[_StatefulIngestor, GraphUpdater]:
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
    return store, updater


def _project(temp_repo: Path, files: dict[str, str]) -> Path:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in files.items():
        _write(root, rel, text)
    return root


@pytest.fixture
def repo(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = _project(temp_repo, FIXTURE)
    store, updater = _index(root)
    return root, store, updater


def _read(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def _smoke(root: Path, expression: str) -> None:
    """Run the rewritten fixture for real: a rewrite that parses can still
    be wrong, and only executing the call sites against the new definition
    shows they agree."""
    result = subprocess.run(
        [sys.executable, "-c", expression],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _set_resolution(store: _StatefulIngestor, caller: str, resolution: str) -> None:
    edge = next(
        e
        for e in store.edges
        if e[1] == caller and e[2] == cs.RelationshipType.CALLS.value and e[4] == HELPER
    )
    store.edge_props[edge][cs.KEY_RESOLUTION] = resolution


# --- acceptance (issue #1533) --------------------------------------------------


def test_add_required_parameter_with_a_default_mapping(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.old_params == ("a", "b")
    assert report.new_params == ("a", "n", "b")
    # The new parameter is REQUIRED in the definition: the literal is what
    # existing sites pass, not a default the definition gains.
    assert "def helper(a: int, n: int, b: str = 'x') -> str:" in _read(
        root, "pkg/util.py"
    )
    app = _read(root, "pkg/app.py")
    assert "return helper(2, 1)" in app
    assert "return helper(2, 1, b='y')" in app
    assert "return helper(3, 1, 'z')" in app
    assert report.unmapped == ()
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    assert [t["qualified_name"] for t in report.verdict.affected_tests] == [
        f"{PROJECT}.tests.test_app.test_run"
    ]
    assert set(report.files) == {"pkg/util.py", "pkg/app.py"}
    _smoke(
        root,
        "from pkg.app import run, run_kw, run_both\n"
        "assert (run(), run_kw(), run_both()) == ('xx', 'yy', 'zzz')\n",
    )


def test_reorder_parameters_rewrites_positional_callers_only(
    temp_repo: Path,
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a: int, b: str) -> str:\n    return b * a\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(2, 'x')\n\n\n"
                "def run_kw():\n    return helper(a=2, b='y')\n"
            ),
        },
    )
    store, updater = _index(root)
    before_kw = "return helper(a=2, b='y')"
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["b", "a"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    # A bare old name carries the old annotation and default over.
    assert "def helper(b: str, a: int) -> str:" in _read(root, "pkg/util.py")
    app = _read(root, "pkg/app.py")
    assert "return helper('x', 2)" in app
    # A keyword caller binds by name and is left exactly as written.
    assert before_kw in app
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(
        root,
        "from pkg.app import run, run_kw\nassert (run(), run_kw()) == ('xx', 'yy')",
    )


def test_heuristic_site_is_listed_as_unmapped_not_rewritten(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
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
    assert skipped.owner == f"{PROJECT}.pkg.app.run_both"
    assert skipped.path == "pkg/app.py"
    assert cs.EdgeResolution.HEURISTIC.value in skipped.reason
    assert [s.owner for s in report.sites if s.kind == "call"] == [
        f"{PROJECT}.pkg.app.run",
        f"{PROJECT}.pkg.app.run_kw",
    ]
    # The diff carries the two rewritten sites and not the guessed one.
    assert "+    return helper(2, 1)" in report.diff
    assert "helper(3, 1, 'z')" not in report.diff
    assert _read(root, "pkg/app.py") == FIXTURE["pkg/app.py"]


def test_allow_heuristic_rewrites_the_guessed_site(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        allow_heuristic=True,
        dry_run=True,
    )
    assert report.unmapped == ()
    assert "helper(3, 1, 'z')" in report.diff


def test_allow_heuristic_applies_through_the_contract(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # The contract refuses a rewritten guessed site unless the operation
    # was told to allow it; the leave must reach the expectation.
    root, store, updater = repo
    _set_resolution(
        store, f"{PROJECT}.pkg.app.run_both", cs.EdgeResolution.HEURISTIC.value
    )
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        allow_heuristic=True,
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    assert "return helper(3, 1, 'z')" in _read(root, "pkg/app.py")


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
        for e in store.edges
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
        for e in store.edges
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


# --- the definition ------------------------------------------------------------------


def test_default_literal_incompatible_with_the_declared_type_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match=r"does not fit"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", "n: int", "b"],
            {"n": "='no'"},
            dry_run=True,
        )
    # The old annotation counts too: `b: str` is carried over by the bare name.
    with pytest.raises(SignatureRefused, match=r"does not fit"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", "b"],
            {"b": "=3"},
            dry_run=True,
        )
    # An optional annotation admits None; a non-literal is not checked.
    for annotation, literal in (
        ("int | None", "=None"),
        ("Optional[int]", "=None"),
        ("int", "=LIMIT"),
        ("Sequence[int]", "=()"),
    ):
        report = change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", f"n: {annotation}", "b"],
            {"n": literal},
            dry_run=True,
        )
        assert report.unmapped == ()


def test_a_kept_parameter_spelled_anew_takes_the_new_spelling(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # `b` exists already; spelling it out again re-annotates it. Only the
    # bare name carries the old spelling over.
    root, store, _updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "b: int = 0"],
        dry_run=True,
    )
    assert "+def helper(a: int, b: int = 0) -> str:" in report.diff
    # Sites keep their values: the parameters did not move.
    assert "helper(2, b='y')" not in report.diff
    # The new annotation is the one a literal is checked against.
    with pytest.raises(SignatureRefused, match=r"does not fit"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", "b: int = 0"],
            {"b": "='q'"},
            dry_run=True,
        )


def test_a_required_parameter_after_a_default_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match=r"after"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            HELPER,
            ["a", "b", "n: int"],
            {"n": "=1"},
        )
    assert _read(root, "pkg/util.py") == FIXTURE["pkg/util.py"]
    assert load_history(root) == []


@pytest.mark.parametrize(
    "header",
    [
        "def helper(a, *rest):",
        "def helper(a, **extra):",
        "def helper(a, /, b):",
        "def helper(a, *, b):",
    ],
)
def test_a_definition_with_special_parameters_is_refused(
    temp_repo: Path, header: str
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": f"{header}\n    return a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
        },
    )
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"positional-or-keyword"):
        change_signature(
            root, store.fetch_all, PROJECT, HELPER, ["a", "n"], dry_run=True
        )


def test_a_non_python_definition_is_refused(temp_repo: Path) -> None:
    root = _project(
        temp_repo,
        {
            "lib/tool.js": "export function helper(a) { return a; }\n",
            "lib/app.js": "import { helper } from './tool.js';\nexport const run = () => helper(1);\n",
        },
    )
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"Python"):
        change_signature(
            root, store.fetch_all, PROJECT, f"{PROJECT}.lib.tool.helper", ["a", "n"]
        )


def test_an_unknown_definition_is_refused(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    with pytest.raises(SignatureRefused, match=r"No definition"):
        change_signature(
            root, store.fetch_all, PROJECT, f"{PROJECT}.pkg.util.nothing", ["a"]
        )


# --- renamed parameters in the body ----------------------------------------------


def _rename_dry_run(temp_repo: Path, body: str) -> str:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": f"def helper(a, b):\n{body}",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(2, 'x')\n",
        },
    )
    store, _updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["times", "text"],
        {"times": "a", "text": "b"},
        dry_run=True,
    )
    return report.diff


def test_a_renamed_parameter_is_renamed_where_the_body_reads_it(
    temp_repo: Path,
) -> None:
    diff = _rename_dry_run(
        temp_repo,
        # A comprehension reads the parameter without re-binding it; an
        # attribute and a keyword argument merely share its spelling.
        "    total = [i * a for i in range(len(b))]\n"
        "    a = a + 1\n"
        "    return b.a(a=a, text=b) or total\n",
    )
    assert "+def helper(times, text):" in diff
    assert "+    total = [i * times for i in range(len(text))]" in diff
    assert "+    times = times + 1" in diff
    assert "+    return text.a(a=times, text=text) or total" in diff


def test_swapping_two_parameter_names_renames_both_ways(temp_repo: Path) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a, b):\n    return b * a\n",
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(2, 'x')\n",
        },
    )
    store, _updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["b", "a"],
        {"b": "a", "a": "b"},
        dry_run=True,
    )
    assert "+def helper(b, a):" in report.diff
    assert "+    return a * b" in report.diff
    # The values stay where they were: only the names moved.
    assert "helper(2, 'x')" not in report.diff


@pytest.mark.parametrize(
    "body",
    [
        "    def inner():\n        return a\n    return inner() * b\n",
        "    return (lambda: a)() * b\n",
        "    return [a for a in range(len(b))]\n",
        "    global a\n    return b\n",
        "    from pkg import a\n    return b\n",
    ],
)
def test_a_parameter_used_in_a_nested_scope_refuses_the_rename(
    temp_repo: Path, body: str
) -> None:
    with pytest.raises(SignatureRefused, match=r"nested scope"):
        _rename_dry_run(temp_repo, body)


@pytest.mark.parametrize(
    "body",
    [
        "    text = 'q'\n    return b * a + text\n",
        # A nested scope reading the new name would start reading the
        # renamed parameter instead of whatever it read before.
        "    def inner():\n        return text\n    return inner() * b + a\n",
        "    return (lambda: times)() * b + a\n",
    ],
)
def test_a_new_name_already_used_in_the_body_is_refused(
    temp_repo: Path, body: str
) -> None:
    with pytest.raises(SignatureRefused, match=r"already used"):
        _rename_dry_run(temp_repo, body)


def test_a_recursive_call_is_rewritten_with_its_renamed_arguments(
    temp_repo: Path,
) -> None:
    # The site's arguments contain the body rename: one edit, not two
    # overlapping ones.
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": (
                "def helper(a):\n    return helper(a - 1) + 1 if a else 0\n"
            ),
            "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(2)\n",
        },
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["times", "n: int"],
        {"times": "a", "n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    util = _read(root, "pkg/util.py")
    assert "def helper(times, n: int):" in util
    assert "    return helper(times - 1, 1) + 1 if times else 0" in util
    assert "return helper(2, 1)" in _read(root, "pkg/app.py")
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(root, "from pkg.app import run\nassert run() == 2")


def test_a_call_nested_in_another_call_is_rewritten_inside_out(
    temp_repo: Path,
) -> None:
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def helper(a):\n    return a + 1\n",
            "pkg/app.py": (
                "from pkg.util import helper\n\n\n"
                "def run():\n    return helper(helper(1))\n"
            ),
        },
    )
    store, updater = _index(root)
    # The eval store keys a CALLS edge on its endpoints, so it holds one
    # site per caller; the real store keys on (line, col) and holds both.
    # Present both sites the way the real store would.
    run_qn = f"{PROJECT}.pkg.app.run"
    stored = store.fetch_all

    def both_sites(query: str, params: dict[str, object] | None = None):  # type: ignore[no-untyped-def]
        rows = stored(query, params)
        out = []
        for row in rows:
            if row.get(cs.KEY_QUALIFIED_NAME) != run_qn:
                out.append(row)
                continue
            for col in (11, 18):
                site = dict(row)
                site[cs.KEY_COL] = col
                site[cs.KEY_END_LINE] = None
                site[cs.KEY_END_COL] = None
                out.append(site)
        return out

    report = change_signature(
        root,
        both_sites,
        PROJECT,
        HELPER,
        ["a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    assert sum(1 for s in report.sites if s.kind == "call") == 2
    assert "return helper(helper(1, 1), 1)" in _read(root, "pkg/app.py")
    # The body still adds one: helper(helper(1, 1), 1) is (1 + 1) + 1.
    _smoke(root, "from pkg.app import run\nassert run() == 3")


# --- hierarchies ---------------------------------------------------------------------


SHAPES: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/shapes.py": (
        "class Base:\n"
        "    def area(self, scale):\n"
        "        return 0 * scale\n\n\n"
        "class Circle(Base):\n"
        "    def area(self, scale):\n"
        "        return 3 * scale\n\n\n"
        "def total(shape: Base):\n"
        "    return shape.area(2) + Circle().area(scale=3)\n"
    ),
}


def test_a_method_hierarchy_is_rewritten_together(temp_repo: Path) -> None:
    root = _project(temp_repo, SHAPES)
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.shapes.Base.area",
        ["scale", "unit: str"],
        {"unit": "='m'"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert set(report.hierarchy) == {
        f"{PROJECT}.pkg.shapes.Base.area",
        f"{PROJECT}.pkg.shapes.Circle.area",
    }
    shapes = _read(root, "pkg/shapes.py")
    assert shapes.count("def area(self, scale, unit: str):") == 2
    assert "shape.area(2, 'm') + Circle().area(scale=3, unit='m')" in shapes
    assert report.verdict is not None, report.message
    assert report.verdict.ok, report.message
    _smoke(root, "from pkg.shapes import total, Circle\nassert total(Circle()) == 15")


def test_an_override_with_different_parameters_is_refused(temp_repo: Path) -> None:
    files = dict(SHAPES)
    files["pkg/shapes.py"] = files["pkg/shapes.py"].replace(
        "    def area(self, scale):\n        return 3 * scale",
        "    def area(self, factor):\n        return 3 * factor",
    )
    root = _project(temp_repo, files)
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"Circle\.area"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.shapes.Base.area",
            ["scale", "unit: str"],
            {"unit": "='m'"},
        )
    assert _read(root, "pkg/shapes.py") == files["pkg/shapes.py"]


def test_a_method_whose_receiver_is_not_self_or_cls_is_refused(
    temp_repo: Path,
) -> None:
    # `this` would be remapped as a parameter and every bound call would
    # lose its receiver.
    files = dict(SHAPES)
    files["pkg/shapes.py"] = files["pkg/shapes.py"].replace(
        "def area(self, scale):", "def area(this, scale):"
    )
    root = _project(temp_repo, files)
    store, _updater = _index(root)
    with pytest.raises(SignatureRefused, match=r"`this`.*self or cls"):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            f"{PROJECT}.pkg.shapes.Base.area",
            ["scale", "unit: str"],
            {"unit": "='m'"},
        )
    assert _read(root, "pkg/shapes.py") == files["pkg/shapes.py"]


@pytest.mark.parametrize("decorator", ["staticmethod", "builtins.staticmethod"])
def test_a_static_method_has_no_receiver(temp_repo: Path, decorator: str) -> None:
    # The indexer reads a decorator by its last name, so the operation must
    # recognise the qualified spelling too.
    root = _project(
        temp_repo,
        {
            "pkg/__init__.py": "",
            "pkg/shapes.py": (
                "import builtins\n\n\n"
                "class K:\n"
                f"    @{decorator}\n"
                "    def helper(a):\n"
                "        return a\n\n\n"
                "def on_class():\n"
                "    return K.helper(2)\n\n\n"
                "def on_instance():\n"
                "    return K().helper(3)\n"
            ),
        },
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        f"{PROJECT}.pkg.shapes.K.helper",
        ["a", "n: int"],
        {"n": "=1"},
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.unmapped == ()
    shapes = _read(root, "pkg/shapes.py")
    assert "    def helper(a, n: int):" in shapes
    assert "return K.helper(2, 1)" in shapes
    assert "return K().helper(3, 1)" in shapes
    _smoke(
        root,
        "from pkg.shapes import on_class, on_instance\n"
        "assert on_class() + on_instance() == 5",
    )


# --- transaction and contract ---------------------------------------------------------


def test_dry_run_reports_the_diff_and_writes_nothing(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        dry_run=True,
    )
    assert not report.applied
    assert "--- a/pkg/util.py" in report.diff
    assert "+def helper(a: int, n: int, b: str = 'x') -> str:" in report.diff
    for rel, text in FIXTURE.items():
        assert _read(root, rel) == text
    assert load_history(root) == []


def test_the_applied_change_is_recorded_for_undo(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = repo
    report = change_signature(
        root, store.fetch_all, PROJECT, HELPER, ["a", "n: int", "b"], {"n": "=1"}
    )
    assert report.applied, report.message
    assert report.verdict is None  # no re-ingest, so no contract
    (entry,) = load_history(root)
    assert entry[cs.EDIT_KEY_ID] == report.transaction_id


def test_a_contract_failure_undoes_the_change(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = repo
    # A caller the graph never saw: the operation cannot rewrite it, and once
    # the re-ingest brings it in, its site passes too few arguments for the
    # new signature without being listed as unmapped. The contract catches
    # it after the fact and the transaction is undone.
    _write(
        root,
        "pkg/late.py",
        "from pkg.util import helper\n\n\ndef late():\n    return helper(2)\n",
    )
    before = {rel: _read(root, rel) for rel in FIXTURE}
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        HELPER,
        ["a", "n: int", "b"],
        {"n": "=1"},
        reingest=lambda paths: updater.reingest([*paths, "pkg/late.py"]),
    )
    assert not report.applied
    assert report.verdict is not None
    assert not report.verdict.ok
    assert "pkg/late.py:5" in report.message
    for rel, text in before.items():
        assert _read(root, rel) == text
    assert load_history(root) == []


# --- MCP tool ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_change_signature_tool_runs_under_the_lock_and_reports(
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
        s
        for s in registry.get_tool_schemas()
        if s.name == cs.MCPToolName.CHANGE_SIGNATURE
    )
    assert set(schema.inputSchema["required"]) == {
        cs.MCPParamName.QUALIFIED_NAME,
        cs.MCPParamName.NEW_PARAMS,
    }
    entry = registry.get_tool_handler(cs.MCPToolName.CHANGE_SIGNATURE)
    assert entry is not None
    payload = await entry[0](
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "=1"},
        dry_run=True,
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert payload["applied"] is False
    assert payload[cs.KEY_SITES]
    assert payload["unmapped"] == []
    assert "helper(2, 1)" in payload["diff"]
    assert _read(root, "pkg/app.py") == FIXTURE["pkg/app.py"]


@pytest.mark.asyncio
async def test_mcp_change_signature_refusal_is_a_payload_not_an_exception(
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
    payload = await registry.change_signature(
        qualified_name=HELPER,
        new_params=["a", "n: int", "b"],
        mapping={"n": "nothing"},
        project=PROJECT,
    )
    assert isinstance(payload, dict)
    assert cs.DICT_KEY_ERROR in payload
    assert "nothing" in payload[cs.DICT_KEY_ERROR]
