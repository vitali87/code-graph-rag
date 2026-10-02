"""Issue #2564: `cgr rename` must not trust an incomplete graph blindly.

The plan comes from graph edges, and the indexer misses call sites in known
ways (#2542 Rust re-exports, #2544 Java static imports, #2546 method refs,
...). A rename that rewrote only the sites it knew reported success and left
every other call under the old name, so the build broke. The plan is now
cross-checked against the source: an occurrence of the old name in call or
reference position that no site of the plan (and no other same-named symbol
the graph knows) accounts for refuses the rename, like a guessed site does,
and `allow_heuristic` rewrites it as one.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from codebase_rag import constants as cs
from codebase_rag import cypher_queries as cq
from codebase_rag.cli import app
from codebase_rag.editing.rename import QueryFn, RenameRefused, rename
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.mcp.tools import MCPToolsRegistry
from codebase_rag.parser_loader import load_parsers
from codebase_rag.types_defs import PropertyParams, ResultRow
from evals.cgr_graph import _StatefulIngestor

PROJECT = "unplanned"
HELPER = f"{PROJECT}.pkg.util.helper"

PY = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def helper(a, b):\n    return a + b\n",
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1, 2)\n",
    "pkg/late.py": "from pkg import util\n\n\ndef later():\n    return util.helper(3, 4)\n",
}
# `    return util.` is 16 bytes: the column of `helper` on line 5.
LATE_SITE = ("call", "pkg/late.py", 5, 16, "unplanned")

RUST = {
    "Cargo.toml": '[package]\nname = "mr"\nversion = "0.1.0"\nedition = "2021"\n',
    "src/lib.rs": "mod parse;\npub use parse::Parse;\npub mod cmd;\n",
    "src/parse.rs": (
        "pub struct Parse {\n"
        "    parts: Vec<String>,\n"
        "}\n"
        "\n"
        "impl Parse {\n"
        "    pub fn new() -> Parse {\n"
        "        Parse { parts: Vec::new() }\n"
        "    }\n"
        "\n"
        "    pub(crate) fn next_string(&mut self) -> Option<String> {\n"
        "        self.parts.pop()\n"
        "    }\n"
        "}\n"
    ),
    "src/cmd/mod.rs": "pub mod get;\n",
    # `Parse` arrives through the crate-root re-export, the shape #2542 misses.
    "src/cmd/get.rs": (
        "use crate::Parse;\n"
        "\n"
        "pub fn key(parse: &mut Parse) -> Option<String> {\n"
        "    // parse.next_string() is the key\n"
        "    let key = parse.next_string()?;\n"
        "    Some(key)\n"
        "}\n"
    ),
}
NEXT_STRING = "mr.src.parse.Parse.next_string"


def _indexed(
    root: Path, files: dict[str, str], project: str = PROJECT
) -> tuple[_StatefulIngestor, GraphUpdater]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    parsers, queries = load_parsers()
    store = _StatefulIngestor()
    updater = GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=project,
    )
    updater.run(force=True)
    return store, updater


def _query(store: _StatefulIngestor) -> QueryFn:
    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        return store.fetch_all(query, None if params is None else dict(params))

    return fetch_all


def _missing(store: _StatefulIngestor, *paths: str) -> QueryFn:
    """The graph as an indexer that missed every call and reference in `paths`."""

    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = store.fetch_all(query, None if params is None else dict(params))
        if query in (cq.CYPHER_GRAPH_CALLERS, cq.CYPHER_GRAPH_REFERENCES):
            return [row for row in rows if row.get(cs.KEY_PATH) not in paths]
        return rows

    return fetch_all


def _tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".cgr" not in path.name
    }


def _site(site: tuple[str, str, int, int, str | None]) -> tuple[str, str, int, int]:
    return site[0], site[1], site[2], site[3]


@pytest.fixture
def py_repo(tmp_path: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, PY)
    return root, store, updater


# --- red: the rename no longer reports success over a site it never saw ------


@pytest.mark.parametrize("dry_run", [False, True], ids=["apply", "dry-run"])
def test_a_call_site_the_graph_missed_refuses_the_rename(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater], dry_run: bool
) -> None:
    root, store, _updater = py_repo
    before = _tree(root)

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/late.py"),
            PROJECT,
            HELPER,
            "assist",
            dry_run=dry_run,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [LATE_SITE]
    assert "pkg/late.py:5" in str(refused.value)
    assert _tree(root) == before


def test_the_rust_reexport_case_never_leaves_a_call_under_the_old_name(
    tmp_path: Path,
) -> None:
    # The issue's own case, with nothing hidden: the indexer itself misses
    # `parse.next_string()` when `Parse` comes through `use crate::Parse`.
    # Should that be fixed, the rename may apply, but only with the call
    # rewritten; it must never apply around it.
    root = tmp_path / "mr"
    root.mkdir()
    store, updater = _indexed(root, RUST, project="mr")

    try:
        report = rename(
            root,
            _query(store),
            "mr",
            NEXT_STRING,
            "next_str",
            reingest=updater.reingest,
        )
    except RenameRefused as refused:
        assert ("call", "src/cmd/get.rs", 5, 20) in {
            (s.kind, s.path, s.line, s.col) for s in refused.unplanned
        }
        assert (root / "src/cmd/get.rs").read_text() == RUST["src/cmd/get.rs"]
        assert (root / "src/parse.rs").read_text() == RUST["src/parse.rs"]
        return
    assert report.applied, report.message
    assert "parse.next_str()?" in (root / "src/cmd/get.rs").read_text()


def test_a_missed_rust_method_call_refuses_and_the_comment_is_not_listed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "mr"
    root.mkdir()
    store, _updater = _indexed(root, RUST, project="mr")

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "src/cmd/get.rs"),
            "mr",
            NEXT_STRING,
            "next_str",
            dry_run=True,
        )

    # Line 4 is the `// parse.next_string()` comment: prose, not a site.
    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "src/cmd/get.rs", 5, 20, "unplanned")]


JAVA = {
    "src/main/java/a/Greeter.java": (
        "package a;\n"
        "\n"
        "public class Greeter {\n"
        "    public static String greet(String n) {\n"
        '        return "hi " + n;\n'
        "    }\n"
        "}\n"
    ),
    "src/main/java/a/Use.java": (
        "package a;\n"
        "\n"
        "import java.util.List;\n"
        "\n"
        "public class Use {\n"
        "    public void all(List<String> xs) {\n"
        "        xs.forEach(Greeter::greet);\n"
        "    }\n"
        "}\n"
    ),
    "src/main/java/b/Stat.java": (
        "package b;\n"
        "\n"
        "import static a.Greeter.greet;\n"
        "\n"
        "public class Stat {\n"
        "    public String go() {\n"
        '        return greet("y");\n'
        "    }\n"
        "}\n"
    ),
}


def test_a_missed_java_method_reference_and_static_import_call_refuse(
    tmp_path: Path,
) -> None:
    # #2546 (method reference) and #2544 (static import): a scoped name and
    # a bare call both name the method.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, JAVA)

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "src/main/java/a/Use.java", "src/main/java/b/Stat.java"),
            PROJECT,
            f"{PROJECT}.src.main.java.a.Greeter.Greeter.greet(String)",
            "welcome",
            dry_run=True,
        )

    assert sorted((s.kind, s.path, s.line, s.col) for s in refused.value.unplanned) == [
        ("call", "src/main/java/b/Stat.java", 7, 15),
        # The static import names the method through its class as well.
        ("reference", "src/main/java/a/Use.java", 7, 28),
        ("reference", "src/main/java/b/Stat.java", 3, 24),
    ]


ERRORS = {
    "pkg/__init__.py": "",
    "pkg/errors.py": "class MyError(Exception):\n    pass\n",
    "pkg/use.py": (
        "from pkg.errors import MyError\n"
        "\n"
        "\n"
        "def go():\n"
        "    try:\n"
        "        return 1\n"
        "    except MyError:\n"
        "        return 0\n"
    ),
}


def test_a_type_is_held_to_every_occurrence_not_only_calls(tmp_path: Path) -> None:
    # `except MyError:` neither calls nor scopes the class, but leaving it
    # under the old name is a NameError the first time something is raised.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, ERRORS)

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/use.py"),
            PROJECT,
            f"{PROJECT}.pkg.errors.MyError",
            "Oops",
            dry_run=True,
        )

    assert [(s.kind, s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("reference", "pkg/use.py", 7, 11)
    ]


@pytest.mark.parametrize(
    ("body", "col"),
    [
        ("    callback = helper\n    return callback(1, 2)\n", 15),
        ("    return list(map(helper, xs, xs))\n", 20),
        ("    return helper\n", 11),
    ],
    ids=["assigned", "passed", "returned"],
)
def test_a_function_named_without_a_call_is_held_to_the_plan(
    tmp_path: Path, body: str, col: int
) -> None:
    # Review of PR #2797: renaming the import and leaving `callback = helper`
    # is a NameError the moment the module loads.
    root = tmp_path / PROJECT
    root.mkdir()
    text = f"from pkg.util import helper\n\n\ndef use(xs):\n{body}"
    store, _updater = _indexed(root, {**PY, "pkg/refs.py": text})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/refs.py"),
            PROJECT,
            HELPER,
            "assist",
            dry_run=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("reference", "pkg/refs.py", 5, col, "unplanned")]


def test_a_call_on_an_import_line_is_not_covered_by_the_import(
    tmp_path: Path,
) -> None:
    # The import site covers its own statement, not the rest of its line.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(
        root, {**PY, "pkg/one.py": "from pkg.util import helper; helper(1, 2)\n"}
    )

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/one.py"),
            PROJECT,
            HELPER,
            "assist",
            dry_run=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/one.py", 1, 29, "unplanned")]


WORKER = {
    "pkg/__init__.py": "",
    "pkg/worker.py": (
        "class Worker:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def run(self):\n"
        "        callback = self.helper\n"
        "        return callback()\n"
        "\n"
        "\n"
        "def peek(other):\n"
        "    return other.helper\n"
    ),
}


def test_a_method_read_through_self_is_held_to_the_plan(tmp_path: Path) -> None:
    # `self.helper` is the method; `other.helper` on anything else is not.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, WORKER)

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/worker.py"),
            PROJECT,
            f"{PROJECT}.pkg.worker.Worker.helper",
            "assist",
            dry_run=True,
        )

    assert [(s.kind, s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("reference", "pkg/worker.py", 6, 24)
    ]


def test_the_refusal_names_ten_locations_and_counts_the_rest(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    calls = "".join(f"    helper({n}, {n})\n" for n in range(12))
    store, _updater = _indexed(
        root, {**PY, "pkg/many.py": f"from pkg import util\n\n\ndef many():\n{calls}"}
    )
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _missing(store, "pkg/many.py"),
            PROJECT,
            HELPER,
            "assist",
            dry_run=True,
        )

    assert len(refused.value.unplanned) == 12
    message = str(refused.value)
    assert "pkg/many.py:14" in message
    assert "pkg/many.py:15" not in message
    assert "and 2 more" in message


def test_a_guessed_and_an_unplanned_site_are_both_reported(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = py_repo
    hidden = _missing(store, "pkg/late.py")

    def guessed(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = hidden(query, params)
        if query != cq.CYPHER_GRAPH_CALLERS:
            return rows
        return [
            {**row, cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC.value}
            for row in rows
        ]

    with pytest.raises(RenameRefused) as refused:
        rename(root, guessed, PROJECT, HELPER, "assist", dry_run=True)

    assert [(s.path, s.line) for s in refused.value.ambiguous] == [("pkg/app.py", 5)]
    assert [(s.path, s.line) for s in refused.value.unplanned] == [("pkg/late.py", 5)]
    assert "1 graph site(s) were also resolved heuristically" in str(refused.value)


def test_allow_heuristic_rewrites_the_unplanned_site_and_reports_it(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = py_repo

    report = rename(
        root,
        _missing(store, "pkg/late.py"),
        PROJECT,
        HELPER,
        "assist",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in report.unplanned
    ] == [LATE_SITE]
    assert _site(LATE_SITE) in {(s.kind, s.path, s.line, s.col) for s in report.sites}
    assert (root / "pkg/late.py").read_text() == PY["pkg/late.py"].replace(
        "util.helper", "util.assist"
    )
    assert "pkg/late.py" in report.files


def test_cli_refusal_lists_the_unplanned_sites_and_exits_nonzero(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = py_repo
    before = _tree(root)
    with (
        patch(
            "codebase_rag.graph_cli._project_and_fetch",
            return_value=(PROJECT, _missing(store, "pkg/late.py"), MagicMock()),
        ),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
    ):
        result = CliRunner().invoke(
            app,
            ["rename", HELPER, "assist", "--repo-path", str(root)],
        )

    assert result.exit_code == 1, result.output
    assert "pkg/late.py" in result.stderr
    assert "unplanned" in result.stderr
    assert _tree(root) == before


def test_cli_payload_lists_what_allow_heuristic_rewrote(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = py_repo
    with (
        patch(
            "codebase_rag.graph_cli._project_and_fetch",
            return_value=(PROJECT, _missing(store, "pkg/late.py"), MagicMock()),
        ),
        patch("codebase_rag.graph_updater.GraphUpdater", return_value=updater),
    ):
        result = CliRunner().invoke(
            app,
            [
                "rename",
                HELPER,
                "assist",
                "--repo-path",
                str(root),
                "--allow-heuristic",
            ],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"] is True
    assert [
        (s["kind"], s["path"], s["line"], s["col"], s["resolution"])
        for s in payload["unplanned"]
    ] == [LATE_SITE]


def test_mcp_refusal_payload_lists_the_unplanned_sites(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = py_repo
    ingestor = MagicMock()
    ingestor.fetch_all = _missing(store, "pkg/late.py")
    ingestor.list_projects.return_value = []
    registry = MCPToolsRegistry.__new__(MCPToolsRegistry)
    registry.ingestor = ingestor
    registry.project_root = str(root)
    registry._live_updater = None
    before = _tree(root)

    payload = registry._run_rename(PROJECT, HELPER, "assist", False, False)

    assert isinstance(payload, dict)
    assert cs.DICT_KEY_ERROR in payload
    assert [
        (s["kind"], s["path"], s["line"], s["col"], s["resolution"])
        for s in payload["unplanned"]
    ] == [LATE_SITE]
    assert _tree(root) == before


# --- negative: what the cross-check must not flag ----------------------------


def test_a_complete_graph_renames_as_before_with_nothing_unplanned(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = py_repo

    report = rename(
        root, _query(store), PROJECT, HELPER, "assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert "util.assist(3, 4)" in (root / "pkg/late.py").read_text()
    assert "return assist(1, 2)" in (root / "pkg/app.py").read_text()


PROSE_AND_LOCALS = (
    '"""Call helper(1, 2) to add."""\n'
    "\n"
    "# helper(3, 4) as well\n"
    'LABEL = "helper(5, 6)"\n'
    "\n"
    "\n"
    "def configure(**kw):\n"
    "    return kw\n"
    "\n"
    "\n"
    "def settings(obj):\n"
    "    helper = 3\n"
    "    obj.helper = helper\n"
    "    return configure(helper=helper)\n"
)


def test_prose_strings_locals_and_attributes_are_not_unplanned(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**PY, "pkg/notes.py": PROSE_AND_LOCALS})

    report = rename(
        root, _query(store), PROJECT, HELPER, "assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/notes.py").read_text() == PROSE_AND_LOCALS


OTHER = (
    "class Other:\n"
    "    def helper(self):\n"
    "        return 0\n"
    "\n"
    "\n"
    "def use():\n"
    "    return Other().helper()\n"
)


def test_a_same_named_symbol_the_graph_knows_is_not_unplanned(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**PY, "pkg/other.py": OTHER})

    report = rename(
        root, _query(store), PROJECT, HELPER, "assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/other.py").read_text() == OTHER


LOCALS = (
    "from pkg.util import helper\n"
    "\n"
    "\n"
    "def apply(helper, xs):\n"
    "    return [helper(x, x) for x in xs] + [helper]\n"
    "\n"
    "\n"
    "def attribute(obj):\n"
    "    return obj.helper\n"
    "\n"
    "\n"
    "def keyword(f):\n"
    "    return f(helper=1)\n"
    "\n"
    "\n"
    "def shadowed():\n"
    "    helper = 3\n"
    "    return helper + 1\n"
)


def test_parameters_attributes_keywords_and_locals_are_not_unplanned(
    tmp_path: Path,
) -> None:
    # The graph knows nothing in this file but its import, and none of these
    # names the function: a parameter and its uses, an attribute of another
    # object, a keyword's name, and a local that shadows the import.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {**PY, "pkg/locals.py": LOCALS})

    report = rename(root, _missing(store, "pkg/locals.py"), PROJECT, HELPER, "assist")

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/locals.py").read_text() == LOCALS.replace(
        "from pkg.util import helper", "from pkg.util import assist"
    )


def test_a_multi_name_import_line_stays_clean(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        **PY,
        "pkg/util.py": (
            "def helper(a, b):\n    return a + b\n\n\ndef other(a):\n    return a\n"
        ),
        "pkg/both.py": (
            "from pkg.util import helper, other\n\n\ndef run():\n"
            "    return other(helper(1, 2))\n"
        ),
    }
    store, updater = _indexed(root, files)

    report = rename(
        root, _query(store), PROJECT, HELPER, "assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/both.py").read_text() == (
        "from pkg.util import assist, other\n\n\ndef run():\n"
        "    return other(assist(1, 2))\n"
    )


def test_another_language_ignored_dirs_and_cgrignore_are_not_scanned(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    outside = {
        # Another language family: a JavaScript `helper` is not this symbol.
        "web/app.js": "export function go() {\n  return helper(1, 2);\n}\n",
        # A built-in ignored directory and a user exclusion: not the project.
        "node_modules/dep/index.py": "def go():\n    return helper(1, 2)\n",
        "vendor/copy.py": "def go():\n    return helper(1, 2)\n",
        ".cgrignore": "vendor/\n",
    }
    store, updater = _indexed(root, {**PY, **outside})

    report = rename(
        root, _query(store), PROJECT, HELPER, "assist", reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    for rel, text in outside.items():
        assert (root / rel).read_text() == text


def test_a_guessed_site_alone_still_refuses_with_the_heuristic_message(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = py_repo

    def guessed(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = store.fetch_all(query, None if params is None else dict(params))
        if query != cq.CYPHER_GRAPH_CALLERS:
            return rows
        return [
            {**row, cs.KEY_RESOLUTION: cs.EdgeResolution.HEURISTIC.value}
            if row.get(cs.KEY_PATH) == "pkg/late.py"
            else row
            for row in rows
        ]

    with pytest.raises(RenameRefused) as refused:
        rename(root, guessed, PROJECT, HELPER, "assist", dry_run=True)

    assert refused.value.unplanned == []
    assert [(s.path, s.line) for s in refused.value.ambiguous] == [("pkg/late.py", 5)]
    assert "heuristically" in str(refused.value)
