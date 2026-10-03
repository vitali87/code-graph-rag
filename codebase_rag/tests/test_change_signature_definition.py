# change_signature (issue #1533): the definition's new parameter list, and
# parameters renamed where the body reads them.
from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.editing.signature import change_signature
from codebase_rag.editing.signature_spec import SignatureRefused
from codebase_rag.editing.transaction import load_history
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
