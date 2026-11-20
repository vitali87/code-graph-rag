"""`check` reports a dangling caller where the call is in the working tree.

The site came from the base graph, so it kept the base line: deleting a
function above its caller reported the call past the end of the file
(`app.py:10` in a 6-line file) or at another function's line, while the
same report's `signature_changes` used working-tree lines (issue #3171).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import StructuralDelta, observe
from evals.cgr_graph import _StatefulIngestor

PROJECT = "chk"
_APP = (
    "def helper():\n    return 1\n\n\n"
    "def other():\n    return 2\n\n\n"
    "def uses_helper():\n    return helper() + other()\n"
)
_ELSEWHERE = "from app import helper\n\n\ndef far():\n    return helper()\n"


def _write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def indexed(tmp_path: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    return _index(tmp_path, _APP)


def _index(
    tmp_path: Path, app: str, elsewhere: str = _ELSEWHERE
) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = tmp_path / PROJECT
    _write(root, "app.py", app)
    _write(root, "far.py", elsewhere)
    store = _StatefulIngestor()
    parsers, queries = load_parsers()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )
    updater.run(force=True)
    return root, store, updater


def _check(
    root: Path, store: _StatefulIngestor, updater: GraphUpdater, changed: list[str]
) -> StructuralDelta:
    return observe(
        store.fetch_all,
        PROJECT,
        changed,
        lambda: updater.reingest(changed, deleted=[]),
        repo_root=root,
    )


def _sites(delta: StructuralDelta) -> dict[str, tuple[int | None, int | None, str]]:
    return {
        d["caller"].removeprefix(f"{PROJECT}."): (
            d["line"],
            d["col"],
            d.get("line_from", ""),
        )
        for d in delta["dangling_callers"]
    }


def test_a_caller_below_a_deleted_function_is_reported_where_it_is_now(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "app.py", _APP.replace("def helper():\n    return 1\n\n\n", ""))
    sites = _sites(_check(root, store, updater, ["app.py"]))
    edited = (root / "app.py").read_text().splitlines()
    assert sites["app.uses_helper"] == (6, 11, ""), sites
    assert edited[5][11:].startswith("helper()"), edited


def test_a_caller_in_an_untouched_file_keeps_its_line(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # Negative: far.py was not edited, so its base line is its line.
    root, store, updater = indexed
    _write(root, "app.py", _APP.replace("def helper():\n    return 1\n\n\n", ""))
    assert _sites(_check(root, store, updater, ["app.py"]))["far.far"] == (5, 11, "")


def test_a_caller_whose_body_changed_says_its_line_is_the_base_one(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # Negative: an edit inside the caller may have moved the call within
    # it, so the line is not shifted and is marked as the base ref's.
    root, store, updater = indexed
    edited = _APP.replace("def helper():\n    return 1\n\n\n", "").replace(
        "    return helper() + other()",
        "    x = other()\n    y = other()\n    return helper() + x + y",
    )
    _write(root, "app.py", edited)
    sites = _sites(_check(root, store, updater, ["app.py"]))
    assert sites["app.uses_helper"] == (10, 11, cs.DANGLING_LINE_FROM_BASE), sites


_HELPER = "def helper():\n    return 1\n\n\n"


@pytest.mark.parametrize(
    ("body", "edited_body"),
    [
        # Greptile, PR #3245: the skeleton drops names, so swapping two
        # calls keeps the fingerprint and the span.
        pytest.param(
            "    a = helper()\n    b = other()\n",
            "    b = other()\n    a = helper()\n",
            id="calls-swapped",
        ),
        # Codex and CodeRabbit, PR #3245: the skeleton drops comments and
        # blank lines, so moving one past the call keeps both too.
        pytest.param(
            "    # first\n    a = helper()\n    b = other()\n",
            "    a = helper()\n    # first\n    b = other()\n",
            id="comment-moved",
        ),
        pytest.param(
            "\n    a = helper()\n    b = other()\n",
            "    a = helper()\n\n    b = other()\n",
            id="blank-line-moved",
        ),
        # CodeRabbit, PR #3245: a comment naming the callee at the call's
        # column can trade lines with the call, so the shifted line reads
        # `helper()`, but inside the comment.
        pytest.param(
            "    a = helper()\n    #   helper()\n    b = other()\n",
            "    #   helper()\n    a = helper()\n    b = other()\n",
            id="comment-swapped",
        ),
        # Nor does the skeleton look inside a string literal: a line it
        # gains can carry the callee's name to the shifted line.
        pytest.param(
            '    b = """\n    """\n    a = helper()\n\n\n',
            '    b = """\n\n        helper()\n    """\n    a = helper()\n',
            id="string-line-moved",
        ),
    ],
)
def test_a_call_moved_inside_an_unchanged_skeleton_says_its_line_is_the_base_one(
    tmp_path: Path, body: str, edited_body: str
) -> None:
    # Negative: the caller's start moved by -4, but the call moved within
    # it, so the shifted line holds no `helper` call.
    head = "def other():\n    return 2\n\n\ndef uses_helper():\n"
    tail = "    return a + b\n"
    root, store, updater = _index(tmp_path, _HELPER + head + body + tail)
    _write(root, "app.py", head + edited_body + tail)

    sites = _sites(_check(root, store, updater, ["app.py"]))

    base_line = 10 + body.split("\n").index("    a = helper()")
    assert sites["app.uses_helper"] == (base_line, 8, cs.DANGLING_LINE_FROM_BASE)


def test_a_call_through_a_qualifier_is_moved_too(tmp_path: Path) -> None:
    # `self.helper()` is recorded at `helper`, its member name (#3166), and
    # moves with its line; a site recorded at `self` reads the qualifier past.
    app = (
        "class Box:\n    def helper(self):\n        return 1\n\n"
        "    def uses(self):\n        return self.helper()\n"
    )
    root, store, updater = _index(tmp_path, app)
    _write(
        root, "app.py", app.replace("    def helper(self):\n        return 1\n\n", "")
    )

    sites = _sites(_check(root, store, updater, ["app.py"]))

    assert sites["app.Box.uses"] == (3, 20, ""), sites


def test_a_call_through_an_import_alias_is_moved_too(tmp_path: Path) -> None:
    # CodeRabbit, PR #3245: `from app import helper as h` spells the call
    # `h()`, not by the definition's name.
    elsewhere = "from app import helper as h\n\n\ndef far():\n    return h()\n"
    root, store, updater = _index(tmp_path, _APP, elsewhere)
    _write(root, "app.py", _APP.replace(_HELPER, ""))
    _write(
        root,
        "far.py",
        elsewhere.replace("def far", "def first():\n    return 0\n\n\ndef far"),
    )

    sites = _sites(_check(root, store, updater, ["app.py", "far.py"]))

    edited = (root / "far.py").read_text().splitlines()
    assert sites["far.far"] == (9, 11, ""), sites
    assert edited[8][11:].startswith("h()"), edited


@pytest.mark.parametrize(
    ("imports", "params", "body", "edited_body"),
    [
        # CodeRabbit, PR #3245: the file imports the target as `a` and as
        # `b`, so either name reads as the call, and the skeleton drops
        # names: `a()` can trade lines with a `b()` the parameter shadows.
        pytest.param(
            "from app import helper as a\nfrom app import helper as b\n",
            "b",
            "    x = a()\n    y = b()\n",
            "    y = b()\n    x = a()\n",
            id="two-aliases",
        ),
        # Imported as `h`, the target's own name calls something else: here
        # the parameter `helper`.
        pytest.param(
            "from app import helper as h\n",
            "helper",
            "    x = h()\n    y = helper()\n",
            "    y = helper()\n    x = h()\n",
            id="own-name-shadowed",
        ),
    ],
)
def test_a_call_traded_with_a_shadowed_spelling_says_its_line_is_the_base_one(
    tmp_path: Path, imports: str, params: str, body: str, edited_body: str
) -> None:
    # Negative: far's start moved by +4, but the call moved within it, so
    # the shifted line holds the parameter's call, not the dangling one.
    head = f"{imports}\n\ndef far({params}):\n"
    tail = "    return x + y\n"
    root, store, updater = _index(tmp_path, _APP, head + body + tail)
    _write(root, "app.py", _APP.replace(_HELPER, ""))
    first = "def first():\n    return 0\n\n\ndef far"
    _write(root, "far.py", head.replace("def far", first) + edited_body + tail)

    sites = _sites(_check(root, store, updater, ["app.py", "far.py"]))

    base_line = head.count("\n") + 1
    assert sites["far.far"] == (base_line, 8, cs.DANGLING_LINE_FROM_BASE), sites
