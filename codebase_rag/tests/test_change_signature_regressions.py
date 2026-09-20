from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag.editing import SignatureRefused, change_signature
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.tests.change_signature_helpers import (
    PROJECT,
    _index,
    _qn,
    _write,
    repo_fixture,
)
from evals.cgr_graph import _StatefulIngestor


@pytest.fixture
def repo(temp_repo: Path):
    return repo_fixture(temp_repo)


# --- review findings: rewrites that committed a broken program ------------------


def test_two_parameters_cannot_share_one_source(
    repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    """A source holds one value per site, so it cannot feed two parameters.

    `_map_arguments` consumes the value: the first mapping took it and every
    later one read as omitted, so the definition gained a parameter no caller
    passed. The edit reported success and the callers raised `TypeError`.
    """
    root, store, updater = repo
    with pytest.raises(SignatureRefused) as excinfo:
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.util.helper"),
            ["left@0", "right@0", "b@1"],
            reingest=updater.reingest,
        )
    assert "left, right" in str(excinfo.value)
    assert (root / "pkg/util.py").read_text().startswith("def helper(a: int")


def test_distinct_sources_are_not_refused_as_duplicates(temp_repo: Path) -> None:
    """The guard must not catch an ordinary rename of every parameter.

    Its own fixture, with no defaulted parameter: the shared one has
    `b: str = 'x'` and a `helper(2)` site relying on it, which a rename leaves
    possibly-missing and the postcondition rightly rolls back. That rollback is
    the operation working correctly, so the shared fixture would be testing
    something other than this guard.
    """
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root, "pkg/util.py", "def helper(a: int, b: str) -> str:\n    return b * a\n"
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(2, 'x')\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["first@0", "second@1"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "def helper(first: int, second: str)" in (root / "pkg/util.py").read_text()


def test_several_new_parameters_are_not_duplicate_sources(temp_repo: Path) -> None:
    """Two parameters with literals share no source: both have none."""
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root, "pkg/util.py", "def helper(a: int, b: str) -> str:\n    return b * a\n"
    )
    _write(
        root,
        "pkg/app.py",
        "from pkg.util import helper\n\n\ndef run():\n    return helper(2, 'x')\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.util.helper"),
        ["a@0", "b@1", "m:int=1", "n:int=2"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert report.new_params == ("a", "b", "m", "n")


def test_override_keyword_callers_use_the_override_s_own_names(
    temp_repo: Path,
) -> None:
    """A caller binds by the name the method it calls declares.

    `old_names` was derived once from the selected definition and reused for
    every hierarchy member's call sites, so an override spelling a parameter
    differently had its keyword callers read as unknown keywords and left
    unrewritten -- against a definition that had been rewritten. The committed
    program then raised `TypeError` for the stale keyword.

    The existing hierarchy test spells the parameter `scale` in both classes,
    which is why this went unseen: the names have to differ for the bug to
    show.
    """
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/shapes.py",
        "class Base:\n    def area(self, scale):\n        return scale\n\n\n"
        "class Square(Base):\n    def area(self, factor):\n        return factor * 4\n"
        "\n\ndef use_square():\n    return Square().area(factor=2)\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.shapes.Base.area"),
        ["size@0"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    text = (root / "pkg/shapes.py").read_text()
    # Both definitions are rewritten, so no caller may still say `factor`.
    assert "factor=2" not in text, text


def test_positional_only_marker_survives_a_rewrite(temp_repo: Path) -> None:
    """`/` is load-bearing on rebuild even though it is not a parameter.

    It was skipped on parse and never re-emitted, so `def target(a, b, /, c)`
    came back as `def target(a, b, c)` and started accepting `target(a=1, b=2,
    c=3)` -- a call the original rejects. Dropping it widens the callable
    contract silently.
    """
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/only.py",
        "def target(a, b, /, c):\n    return a + b + c\n\n\n"
        "def call():\n    return target(1, 2, 3)\n",
    )
    store, updater = _index(root)
    report = change_signature(
        root,
        store.fetch_all,
        PROJECT,
        _qn("pkg.only.target"),
        ["a@0", "b@1", "c@2", "d:int=4"],
        reingest=updater.reingest,
    )
    assert report.applied, report.message
    assert "/" in (root / "pkg/only.py").read_text().split("\n")[0]


def test_a_rewrite_across_the_positional_only_boundary_is_refused(
    temp_repo: Path,
) -> None:
    """Reordering across `/` changes which arguments may be named.

    That is a question this operation is not asked to answer, so it refuses
    rather than emitting a signature whose contract differs from the one the
    caller requested.
    """
    root = temp_repo / PROJECT
    root.mkdir()
    _write(root, "pkg/__init__.py", "")
    _write(
        root,
        "pkg/only.py",
        "def target(a, b, /, c):\n    return a + b + c\n\n\n"
        "def call():\n    return target(1, 2, 3)\n",
    )
    store, updater = _index(root)
    with pytest.raises(SignatureRefused):
        change_signature(
            root,
            store.fetch_all,
            PROJECT,
            _qn("pkg.only.target"),
            ["b@1", "a@0", "c@2"],
            reingest=updater.reingest,
        )
