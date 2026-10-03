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
from codebase_rag.editing.occurrences import (
    Target,
    _Access,
    _access,
    find_occurrences,
)
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

    graph = _missing(store, "pkg/late.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "src/cmd/get.rs")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "src/main/java/a/Use.java", "src/main/java/b/Stat.java")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "pkg/refs.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "pkg/one.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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

    graph = _missing(store, "pkg/worker.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.pkg.worker.Worker.helper",
            "assist",
            dry_run=True,
        )

    assert [(s.kind, s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("reference", "pkg/worker.py", 6, 24)
    ]


CACHE = {
    "pkg/__init__.py": "",
    "pkg/cache.py": "class Cache:\n    def get(self, key):\n        return key\n",
    "pkg/use.py": (
        "from pkg.cache import Cache\n"
        "\n"
        "\n"
        "def read(d, key):\n"
        "    c = Cache()\n"
        "    return c.get(key), d.get(key)\n"
    ),
}
CACHE_GET = f"{PROJECT}.pkg.cache.Cache.get"


def test_a_call_through_an_unknown_object_refuses_even_with_allow_heuristic(
    tmp_path: Path,
) -> None:
    # Review of PR #2797: `d.get(key)` may be a dict's, and rewriting it as a
    # guessed site broke code the postcondition cannot see. `c` is built as
    # a Cache, so `c.get` is certain; `d` is unknown, so nothing is written.
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, CACHE)
    before = _tree(root)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            allow_heuristic=True,
            reingest=updater.reingest,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [
        ("call", "pkg/use.py", 6, 13, "unplanned"),
        ("call", "pkg/use.py", 6, 25, "receiver_unknown"),
    ]
    assert "check them by hand" in str(refused.value)
    assert _tree(root) == before


def test_without_allow_heuristic_the_refusal_says_which_calls_it_cannot_take(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, CACHE)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            dry_run=True,
        )

    message = str(refused.value)
    assert "2 occurrence(s) of get" in message
    assert "1 of them may name another symbol (a call through an object" in message


def test_allow_heuristic_rewrites_a_call_on_a_receiver_declared_as_the_class(
    tmp_path: Path,
) -> None:
    # The issue's own shape: `parse: &mut Parse` shows what `parse` is, so
    # the missed call is rewritten with the rest.
    root = tmp_path / "mr"
    root.mkdir()
    store, updater = _indexed(root, RUST, project="mr")

    report = rename(
        root,
        _missing(store, "src/cmd/get.rs"),
        "mr",
        NEXT_STRING,
        "next_str",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.resolution) for s in report.unplanned] == [
        ("src/cmd/get.rs", 5, "unplanned")
    ]
    assert "let key = parse.next_str()?;" in (root / "src/cmd/get.rs").read_text()
    # The comment is prose, and stays as written.
    assert "// parse.next_string() is the key" in (root / "src/cmd/get.rs").read_text()


HANDLERS = (
    "from pkg.errors import MyError\n"
    "\n"
    "\n"
    "def on_error(e):\n"
    "    return 0\n"
    "\n"
    "\n"
    "HANDLERS = {MyError: on_error}\n"
)


def test_a_class_used_as_a_python_dict_key_is_held_to_the_plan(
    tmp_path: Path,
) -> None:
    # Review of PR #2797: a Python dict key is evaluated, so leaving it
    # under the old name is a NameError when the module loads.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "pkg/__init__.py": "",
        "pkg/errors.py": ERRORS["pkg/errors.py"],
        "pkg/handlers.py": HANDLERS,
    }
    store, _updater = _indexed(root, files)

    graph = _missing(store, "pkg/handlers.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.pkg.errors.MyError",
            "Oops",
            dry_run=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("reference", "pkg/handlers.py", 8, 12, "unplanned")]


def test_the_refusal_names_ten_locations_and_counts_the_rest(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    calls = "".join(f"    helper({n}, {n})\n" for n in range(12))
    store, _updater = _indexed(
        root,
        {**PY, "pkg/many.py": f"from pkg.util import helper\n\n\ndef many():\n{calls}"},
    )
    graph = _missing(store, "pkg/many.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
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


FUNCTIONS = {
    "pkg/__init__.py": "",
    "pkg/util.py": "def get(a):\n    return a\n\n\ndef run(a):\n    return a\n",
    "pkg/app.py": (
        "from pkg.util import get, run\n\n\ndef go():\n    return get(1), run(2)\n"
    ),
    "pkg/client.py": (
        "import subprocess\n"
        "\n"
        "\n"
        "def fetch(d, key):\n"
        '    subprocess.run(["true"])\n'
        "    return d.get(key)\n"
    ),
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize(("name", "new_name"), [("get", "fetch"), ("run", "execute")])
def test_other_objects_methods_are_not_a_functions_occurrences(
    tmp_path: Path, name: str, new_name: str, allow_heuristic: bool
) -> None:
    # Review of PR #2797: a project function `get` or `run` is reached
    # through its module only; `d.get(key)` and `subprocess.run(...)` are
    # not it, and must neither refuse the rename nor be rewritten by it.
    # (The indexer links them to the function by name as a guess of its
    # own; hidden here, they are what the cross-check alone sees.)
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, FUNCTIONS)

    report = rename(
        root,
        _missing(store, "pkg/client.py"),
        PROJECT,
        f"{PROJECT}.pkg.util.{name}",
        new_name,
        allow_heuristic=allow_heuristic,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/client.py").read_text() == FUNCTIONS["pkg/client.py"]
    assert f"{new_name}(" in (root / "pkg/app.py").read_text()


def test_a_function_reached_through_a_module_alias_is_held_to_the_plan(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    aliased = "import pkg.util as u\n\n\ndef go():\n    return u.helper(1, 2)\n"
    store, _updater = _indexed(root, {**PY, "pkg/aliased.py": aliased})

    graph = _missing(store, "pkg/aliased.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            HELPER,
            "assist",
            dry_run=True,
        )

    assert [(s.kind, s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("call", "pkg/aliased.py", 5, 13)
    ]


UNRELATED = (
    "class Store(dict):\n"
    "    def first(self, key):\n"
    "        return self.get(key)\n"
    "\n"
    "\n"
    "def read(d, key):\n"
    "    return d.get(key)\n"
)


def test_a_file_that_never_names_the_class_is_not_held_to_its_method(
    tmp_path: Path,
) -> None:
    # Neither `self.get` in a class that does not extend Cache nor `d.get`
    # can be Cache.get in a file that never names Cache.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**CACHE, "pkg/use.py": "", "pkg/store.py": UNRELATED}
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, "pkg/store.py"),
        PROJECT,
        CACHE_GET,
        "fetch",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/store.py").read_text() == UNRELATED


JS = {
    "package.json": '{"name": "web", "type": "module"}\n',
    "src/util.js": "export function helper(a, b) {\n  return a + b;\n}\n",
    "src/app.js": (
        "import { helper } from './util.js';\n"
        "\n"
        "export const options = { helper: 1 };\n"
        "export const table = { helper };\n"
    ),
}


def test_a_js_key_is_a_label_and_shorthand_is_a_use(tmp_path: Path) -> None:
    # `{ helper: 1 }` names a property; `{ helper }` reads the function.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, JS)

    graph = _missing(store, "src/app.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.src.util.helper",
            "assist",
            dry_run=True,
        )

    assert [(s.kind, s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("reference", "src/app.js", 4, 23)
    ]


# --- review of PR #2797, third round ----------------------------------------

FACTORY = {
    "pkg/__init__.py": "",
    "pkg/cache.py": (
        "class Cache:\n"
        "    def get(self, key):\n"
        "        return key\n"
        "\n"
        "\n"
        "def make_cache():\n"
        "    return Cache()\n"
    ),
}
# `    return cache.` is 17 bytes: the column of `get` on line 6.
FACTORY_USERS = {
    "from-import": (
        "from pkg.cache import make_cache\n"
        "\n"
        "\n"
        "def read(key):\n"
        "    cache = make_cache()\n"
        "    return cache.get(key)\n"
    ),
    "module-alias": (
        "import pkg.cache as pc\n"
        "\n"
        "\n"
        "def read(key):\n"
        "    cache = pc.make_cache()\n"
        "    return cache.get(key)\n"
    ),
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize("user", sorted(FACTORY_USERS))
def test_a_call_on_an_object_from_the_classs_module_is_held_to_the_plan(
    tmp_path: Path, user: str, allow_heuristic: bool
) -> None:
    # Review of PR #2797: `cache` comes from a factory, so the file never
    # names Cache, but it imports from the module that defines it. The call
    # may be Cache.get and is held to the plan; nothing shows what `cache`
    # is, so it is never rewritten either.
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**FACTORY, "pkg/use.py": FACTORY_USERS[user]})
    before = _tree(root)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            allow_heuristic=allow_heuristic,
            reingest=updater.reingest,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/use.py", 6, 17, "receiver_unknown")]
    assert _tree(root) == before


def test_a_module_that_only_shares_a_prefix_with_the_classs_is_not_held(
    tmp_path: Path,
) -> None:
    # `pkg.cache_utils` is not `pkg.cache`: `d.get` there stays out.
    root = tmp_path / PROJECT
    root.mkdir()
    store_py = (
        "from pkg.cache_utils import size\n"
        "\n"
        "\n"
        "def read(d, key):\n"
        "    return d.get(key), size(d)\n"
    )
    files = {
        **FACTORY,
        "pkg/cache_utils.py": "def size(d):\n    return len(d)\n",
        "pkg/store.py": store_py,
    }
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, "pkg/store.py"),
        PROJECT,
        CACHE_GET,
        "fetch",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/store.py").read_text() == store_py


SCOPED_IMPORT = (
    "from pkg.util import helper\n"
    "\n"
    "\n"
    "def local():\n"
    "    from pkg.other import helper\n"
    "\n"
    "    return helper()\n"
    "\n"
    "\n"
    "def outer():\n"
    "    return helper(1, 2)\n"
)
OTHER_HELPER = "def helper():\n    return 0\n"


def test_a_function_scoped_import_of_a_namesake_binds_only_that_function(
    tmp_path: Path,
) -> None:
    # Review of PR #2797: `from pkg.other import helper` inside `local`
    # makes `helper` the other function there, and nowhere else. The call
    # in `outer` is ours, and the graph has no site for it.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**PY, "pkg/other.py": OTHER_HELPER, "pkg/mixed.py": SCOPED_IMPORT}
    store, _updater = _indexed(root, files)
    before = _tree(root)

    graph = _missing(store, "pkg/mixed.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            HELPER,
            "assist",
        )

    # The indexer keeps one binding per name and file, so the graph has the
    # function's import and not line 1's: both of ours are listed. Line 7,
    # the other function's call inside `local`, is not.
    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [
        ("reference", "pkg/mixed.py", 1, 21, "unplanned"),
        ("call", "pkg/mixed.py", 11, 11, "unplanned"),
    ]
    assert _tree(root) == before


def test_a_module_level_import_of_a_namesake_still_binds_the_whole_file(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    elsewhere = (
        "from pkg.other import helper\n"
        "\n"
        "\n"
        "def a():\n"
        "    return helper()\n"
        "\n"
        "\n"
        "def b():\n"
        "    return helper()\n"
    )
    files = {**PY, "pkg/other.py": OTHER_HELPER, "pkg/elsewhere.py": elsewhere}
    store, updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, "pkg/elsewhere.py"),
        PROJECT,
        HELPER,
        "assist",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/elsewhere.py").read_text() == elsewhere


RIVAL = {
    **CACHE,
    "pkg/other.py": "class Cache:\n    def get(self, key):\n        return key\n",
}
# Each reads `get` from the other module's Cache; the second element is the
# (line, col) of `get`.
RIVAL_USERS = {
    "qualified-constructor": (
        "from pkg import other\n"
        "\n"
        "\n"
        "def read(key):\n"
        "    o = other.Cache()\n"
        "    return o.get(key)\n",
        (6, 13),
    ),
    "qualified-annotation": (
        "from pkg import other\n\n\ndef read(o: other.Cache, key):\n"
        "    return o.get(key)\n",
        (5, 13),
    ),
    "qualified-class": (
        "from pkg import other\n\n\ndef read(o, key):\n"
        "    return other.Cache.get(o, key)\n",
        (5, 23),
    ),
    "imported-from-elsewhere": (
        "from pkg.other import Cache\n"
        "\n"
        "\n"
        "def read(key):\n"
        "    o = Cache()\n"
        "    return o.get(key)\n",
        (6, 13),
    ),
    "subclass-of-the-other": (
        "from pkg import other\n"
        "\n"
        "\n"
        "class Sub(other.Cache):\n"
        "    def read(self, key):\n"
        "        return self.get(key)\n",
        (6, 20),
    ),
}


@pytest.mark.parametrize("user", sorted(RIVAL_USERS))
def test_another_modules_same_named_class_is_not_the_target(
    tmp_path: Path, user: str
) -> None:
    # Review of PR #2797: `Cache` alone does not say which Cache. One built,
    # declared, extended or imported from another module is not the target,
    # so `allow_heuristic` must not rewrite a call through it.
    text, (line, col) = RIVAL_USERS[user]
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**RIVAL, "pkg/use.py": text})
    before = _tree(root)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            allow_heuristic=True,
            reingest=updater.reingest,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/use.py", line, col, "receiver_unknown")]
    assert _tree(root) == before


OWN_USERS = {
    "module-qualified": (
        "from pkg import cache\n\n\ndef read(key):\n"
        "    c = cache.Cache()\n    return c.get(key)\n"
    ),
    "module-alias": (
        "import pkg.cache as pc\n\n\ndef read(key):\n"
        "    c = pc.Cache()\n    return c.get(key)\n"
    ),
    "imported": (
        "from pkg.cache import Cache\n\n\ndef read(key):\n"
        "    c = Cache()\n    return c.get(key)\n"
    ),
}


@pytest.mark.parametrize("user", sorted(OWN_USERS))
def test_the_targets_class_is_still_certain_beside_a_namesake(
    tmp_path: Path, user: str
) -> None:
    # With another Cache in the project, a Cache reached through the
    # target's own module is still the target, and the call is rewritten.
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**RIVAL, "pkg/use.py": OWN_USERS[user]})

    report = rename(
        root,
        _missing(store, "pkg/use.py"),
        PROJECT,
        CACHE_GET,
        "fetch",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col, s.resolution) for s in report.unplanned] == [
        ("pkg/use.py", 6, 13, "unplanned")
    ]
    assert "return c.fetch(key)" in (root / "pkg/use.py").read_text()
    assert (root / "pkg/other.py").read_text() == RIVAL["pkg/other.py"]


JS_FACTORY = {
    "package.json": '{"name": "web", "type": "module"}\n',
    "src/cache.js": (
        "export class Cache {\n"
        "  get(key) {\n"
        "    return key;\n"
        "  }\n"
        "}\n"
        "\n"
        "export function makeCache() {\n"
        "  return new Cache();\n"
        "}\n"
    ),
    "src/use.js": (
        "import { makeCache } from './cache.js';\n"
        "\n"
        "export function read(key) {\n"
        "  const cache = makeCache();\n"
        "  return cache.get(key);\n"
        "}\n"
    ),
}


def test_a_module_spelled_in_a_string_is_still_the_classs_module(
    tmp_path: Path,
) -> None:
    # JavaScript names the module in a string, which is no identifier.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, JS_FACTORY)

    graph = _missing(store, "src/use.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.src.cache.Cache.get",
            "fetch",
            dry_run=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "src/use.js", 5, 15, "receiver_unknown")]


def _greeter(package: str) -> str:
    return (
        f"package {package};\n"
        "\n"
        "public class Greeter {\n"
        "    public String greet(String n) {\n"
        "        return n;\n"
        "    }\n"
        "}\n"
    )


def _greeter_user(package: str) -> str:
    return (
        "package c;\n"
        "\n"
        f"import {package}.Greeter;\n"
        "\n"
        "public class Use {\n"
        "    public String go() {\n"
        "        Greeter g = new Greeter();\n"
        '        return g.greet("y");\n'
        "    }\n"
        "}\n"
    )


@pytest.mark.parametrize(
    ("package", "resolution"), [("a", "unplanned"), ("b", "receiver_unknown")]
)
def test_a_java_class_is_told_from_its_namesake_by_its_package(
    tmp_path: Path, package: str, resolution: str
) -> None:
    # A Java file is named after its class, so the import's package is what
    # says which Greeter `g` is: `a.Greeter` is the target, `b.Greeter` not.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "src/main/java/a/Greeter.java": _greeter("a"),
        "src/main/java/b/Greeter.java": _greeter("b"),
        "src/main/java/c/Use.java": _greeter_user(package),
    }
    store, _updater = _indexed(root, files)

    try:
        report = rename(
            root,
            _missing(store, "src/main/java/c/Use.java"),
            PROJECT,
            f"{PROJECT}.src.main.java.a.Greeter.Greeter.greet(String)",
            "welcome",
            allow_heuristic=True,
            dry_run=True,
        )
        unplanned = list(report.unplanned)
    except RenameRefused as refused:
        unplanned = refused.unplanned

    assert [(s.kind, s.path, s.line, s.col, s.resolution) for s in unplanned] == [
        ("call", "src/main/java/c/Use.java", 8, 17, resolution)
    ]


# --- review of PR #2797, fourth round ---------------------------------------

VENDOR = {
    "vendor/__init__.py": "",
    "vendor/cache.py": "def thing():\n    return {}\n",
}
# Each imports from `vendor.cache`, a module that only shares its name with
# the class's `pkg.cache`, and reads a dict.
VENDOR_USERS = {
    "from-import": (
        "from vendor.cache import thing\n\n\ndef read(key):\n"
        "    d = thing()\n    return d.get(key)\n"
    ),
    "module-alias": (
        "import vendor.cache as vc\n\n\ndef read(key):\n"
        "    d = vc.thing()\n    return d.get(key)\n"
    ),
    "module-import": (
        "from vendor import cache\n\n\ndef read(key):\n"
        "    d = cache.thing()\n    return d.get(key)\n"
    ),
}


@pytest.mark.parametrize("user", sorted(VENDOR_USERS))
def test_a_namesake_module_does_not_hold_the_file_to_the_method(
    tmp_path: Path, user: str
) -> None:
    # Review of PR #2797: `vendor.cache` is not `pkg.cache`, so `d.get` is
    # no call of Cache.get and must not block the rename.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**FACTORY, **VENDOR, "pkg/use.py": VENDOR_USERS[user]}
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, "pkg/use.py"),
        PROJECT,
        CACHE_GET,
        "fetch",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "pkg/use.py").read_text() == VENDOR_USERS[user]


# Each imports from the class's own module; `get` is at (6, 17).
OWN_MODULE_USERS = {
    "relative": (
        "from .cache import make_cache\n\n\ndef read(key):\n"
        "    cache = make_cache()\n    return cache.get(key)\n"
    ),
    "module-import": (
        "from pkg import cache as c\n\n\ndef read(key):\n"
        "    cache = c.make_cache()\n    return cache.get(key)\n"
    ),
}


@pytest.mark.parametrize("user", sorted(OWN_MODULE_USERS))
def test_an_import_of_the_classs_own_module_still_holds_the_file(
    tmp_path: Path, user: str
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**FACTORY, **VENDOR, "pkg/use.py": OWN_MODULE_USERS[user]}
    store, _updater = _indexed(root, files)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/use.py", 6, 17, "receiver_unknown")]


TWIN = {
    "pkg1/__init__.py": "",
    "pkg1/cache.py": "class Cache:\n    def get(self, key):\n        return key\n",
    "pkg2/__init__.py": "",
    "pkg2/cache.py": "class Cache:\n    def get(self, key):\n        return key\n",
}
TWIN_GET = f"{PROJECT}.pkg1.cache.Cache.get"
# A Cache from `pkg2.cache`, whose module has the same name as the target's.
TWIN_USERS = {
    "imported": (
        "from pkg2.cache import Cache\n\n\ndef read(key):\n"
        "    o = Cache()\n    return o.get(key)\n"
    ),
    "module-qualified": (
        "from pkg2 import cache\n\n\ndef read(key):\n"
        "    o = cache.Cache()\n    return o.get(key)\n"
    ),
    "fully-qualified": (
        "import pkg2.cache\n\n\ndef read(key):\n"
        "    o = pkg2.cache.Cache()\n    return o.get(key)\n"
    ),
}


@pytest.mark.parametrize("user", sorted(TWIN_USERS))
def test_a_same_named_class_in_a_same_named_module_is_not_the_target(
    tmp_path: Path, user: str
) -> None:
    # `pkg2/cache.py` and `pkg1/cache.py` share the module name `cache`, so
    # the name alone cannot say which Cache an import brings in.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {**TWIN, "app/use.py": TWIN_USERS[user]})
    before = _tree(root)

    graph = _missing(store, "app/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            TWIN_GET,
            "fetch",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "app/use.py", 6, 13, "receiver_unknown")]
    assert _tree(root) == before


def test_the_twin_module_of_the_target_is_still_the_target(tmp_path: Path) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    user = (
        "from pkg1.cache import Cache\n\n\ndef read(key):\n"
        "    o = Cache()\n    return o.get(key)\n"
    )
    store, updater = _indexed(root, {**TWIN, "app/use.py": user})

    report = rename(
        root,
        _missing(store, "app/use.py"),
        PROJECT,
        TWIN_GET,
        "fetch",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col, s.resolution) for s in report.unplanned] == [
        ("app/use.py", 6, 13, "unplanned")
    ]
    assert "return o.fetch(key)" in (root / "app/use.py").read_text()
    assert (root / "pkg2/cache.py").read_text() == TWIN["pkg2/cache.py"]


SHADOWED_ELSEWHERE = (
    "from pkg.cache import Cache\n"
    "\n"
    "\n"
    "def build(Cache):\n"
    "    return Cache()\n"
    "\n"
    "\n"
    "def read(key):\n"
    "    c = Cache()\n"
    "    return c.get(key)\n"
)


def test_a_parameter_named_like_the_class_shadows_it_only_in_its_function(
    tmp_path: Path,
) -> None:
    # Review of PR #2797: `Cache` is a parameter in `build` only; in `read`
    # it is the imported class, so the call is certain and rewritten.
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**CACHE, "pkg/use.py": SHADOWED_ELSEWHERE})

    report = rename(
        root,
        _missing(store, "pkg/use.py"),
        PROJECT,
        CACHE_GET,
        "fetch",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col, s.resolution) for s in report.unplanned] == [
        ("pkg/use.py", 10, 13, "unplanned")
    ]
    assert "return c.fetch(key)" in (root / "pkg/use.py").read_text()


def test_a_parameter_named_like_the_class_shadows_it_in_its_own_function(
    tmp_path: Path,
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    shadowed = (
        "from pkg.cache import Cache\n\n\ndef read(Cache, key):\n"
        "    c = Cache()\n    return c.get(key)\n"
    )
    store, _updater = _indexed(root, {**CACHE, "pkg/use.py": shadowed})
    before = _tree(root)

    graph = _missing(store, "pkg/use.py")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            CACHE_GET,
            "fetch",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/use.py", 6, 13, "receiver_unknown")]
    assert _tree(root) == before


# --- review of PR #2797, fifth round ----------------------------------------

# A project function named like a builtin or a global, its importer, and a
# file that calls the builtin: (files, function, new name, that file).
BUILTIN_NAMESAKES = {
    "python-sorted": (
        {
            "pkg/__init__.py": "",
            "pkg/util.py": "def sorted(xs):\n    return xs\n",
            "pkg/app.py": (
                "from pkg.util import sorted\n\n\ndef go():\n    return sorted([2, 1])\n"
            ),
            "pkg/other.py": "def order(xs):\n    return sorted(xs)\n",
        },
        f"{PROJECT}.pkg.util.sorted",
        "arrange",
        "pkg/other.py",
    ),
    "js-fetch": (
        {
            "package.json": '{"name": "web", "type": "module"}\n',
            "src/net.js": "export function fetch(url) {\n  return url;\n}\n",
            "src/app.js": (
                "import { fetch } from './net.js';\n\n"
                "export function go() {\n  return fetch('/a');\n}\n"
            ),
            "src/page.js": "export function load() {\n  return fetch('/b');\n}\n",
        },
        f"{PROJECT}.src.net.fetch",
        "request",
        "src/page.js",
    ),
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize("case", sorted(BUILTIN_NAMESAKES))
def test_a_builtin_named_like_the_function_is_not_its_use(
    tmp_path: Path, case: str, allow_heuristic: bool
) -> None:
    # Review of PR #2797: `sorted(xs)` in a file that never imports the
    # project's `sorted` is the builtin; a bare name in Python or JS reaches
    # another file's function only through an import.
    files, qn, new_name, builtin_user = BUILTIN_NAMESAKES[case]
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, builtin_user),
        PROJECT,
        qn,
        new_name,
        allow_heuristic=allow_heuristic,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / builtin_user).read_text() == files[builtin_user]


# The same file with the project's `sorted` reached three ways: the second
# element is the resolution the bare call at (5, 11) gets.
SORTED_USERS = {
    "imported": ("from pkg.util import sorted\n\n\ndef order(xs):\n", "unplanned"),
    "star-from-its-module": (
        "from pkg.util import *\n\n\ndef order(xs):\n",
        "unplanned",
    ),
    "star-from-elsewhere": (
        "from os.path import *\n\n\ndef order(xs):\n",
        "receiver_unknown",
    ),
}


@pytest.mark.parametrize("user", sorted(SORTED_USERS))
def test_a_bare_call_reached_through_an_import_still_counts(
    tmp_path: Path, user: str
) -> None:
    head, resolution = SORTED_USERS[user]
    files, qn, new_name, _builtin_user = BUILTIN_NAMESAKES["python-sorted"]
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(
        root, {**files, "pkg/other.py": f"{head}    return sorted(xs)\n"}
    )

    graph = _missing(store, "pkg/other.py")
    with pytest.raises(RenameRefused) as refused:
        rename(root, graph, PROJECT, qn, new_name, dry_run=True)

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/other.py", 5, 11, resolution)]


def test_a_bare_call_in_the_functions_own_file_still_counts(tmp_path: Path) -> None:
    files, qn, new_name, _builtin_user = BUILTIN_NAMESAKES["python-sorted"]
    own = "def sorted(xs):\n    return xs\n\n\ndef again(xs):\n    return sorted(xs)\n"
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {**files, "pkg/util.py": own})

    graph = _missing(store, "pkg/util.py")
    with pytest.raises(RenameRefused) as refused:
        rename(root, graph, PROJECT, qn, new_name, dry_run=True)

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "pkg/util.py", 6, 11, "unplanned")]


def _function_occurrences(
    root: Path, files: dict[str, str], name: str, language: cs.SupportedLanguage
) -> list[tuple[str, int, bool]]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    definition = next(iter(files))
    target = Target(
        name,
        language,
        cs.RenameTargetKind.FUNCTION,
        definition,
        frozenset(),
        frozenset({definition}),
    )
    return [
        (found.path, found.line, found.certain)
        for found in find_occurrences(root, target)
    ]


def test_a_go_call_in_the_same_package_needs_no_import(tmp_path: Path) -> None:
    files = {
        "pkg/util.go": "package pkg\n\nfunc Helper() int {\n\treturn 1\n}\n",
        "pkg/use.go": "package pkg\n\nfunc Use() int {\n\treturn Helper()\n}\n",
    }

    found = _function_occurrences(tmp_path, files, "Helper", cs.SupportedLanguage.GO)

    assert ("pkg/use.go", 4, True) in found


RUST_FUNCTION_USERS = {
    "named-use": ("use crate::util::helper;\n", True),
    "glob-use": ("use crate::util::*;\n", True),
    "glob-elsewhere": ("use std::cmp::*;\n", False),
}


@pytest.mark.parametrize("user", sorted(RUST_FUNCTION_USERS))
def test_a_rust_bare_call_follows_its_use(tmp_path: Path, user: str) -> None:
    head, certain = RUST_FUNCTION_USERS[user]
    files = {
        "src/util.rs": "pub fn helper() -> i32 {\n    1\n}\n",
        "src/lib.rs": "mod util;\nmod app;\n",
        "src/app.rs": f"{head}\npub fn go() -> i32 {{\n    helper()\n}}\n",
    }

    found = _function_occurrences(tmp_path, files, "helper", cs.SupportedLanguage.RUST)

    assert ("src/app.rs", 4, certain) in found


def test_a_rust_bare_call_without_a_use_is_not_the_function(tmp_path: Path) -> None:
    files = {
        "src/util.rs": "pub fn helper() -> i32 {\n    1\n}\n",
        "src/lib.rs": "mod util;\nmod app;\n",
        "src/app.rs": "pub fn go() -> i32 {\n    helper()\n}\n",
    }

    found = _function_occurrences(tmp_path, files, "helper", cs.SupportedLanguage.RUST)

    assert [found for found in found if found[0] == "src/app.rs"] == []


# --- review of PR #2797, sixth round ----------------------------------------

_CACHE_CLASS = "class Cache:\n    def get(self, key):\n        return key\n"
# `pkg.cache` under two source roots: the repository's and `src/`.
TWO_ROOTS = {
    "pkg/__init__.py": "",
    "pkg/cache.py": _CACHE_CLASS,
    "src/pkg/__init__.py": "",
    "src/pkg/cache.py": _CACHE_CLASS,
}
_ROOT_USER = (
    "from pkg.cache import Cache\n\n\ndef read(key):\n"
    "    c = Cache()\n    return c.get(key)\n"
)


@pytest.mark.parametrize(
    ("target", "user"),
    [
        (f"{PROJECT}.pkg.cache.Cache.get", "app/use.py"),
        (f"{PROJECT}.src.pkg.cache.Cache.get", "src/app/use.py"),
        (f"{PROJECT}.src.pkg.cache.Cache.get", "app/use.py"),
        (f"{PROJECT}.pkg.cache.Cache.get", "src/app/use.py"),
    ],
    ids=["root-target", "src-target", "root-import-of-src", "src-import-of-root"],
)
def test_an_import_two_source_roots_satisfy_is_never_rewritten(
    tmp_path: Path, target: str, user: str
) -> None:
    # Review of PR #2797: `pkg/cache.py` and `src/pkg/cache.py` are both
    # spelled `pkg.cache`, and which one Python loads depends on the order
    # of its import path, which the source does not say. The call may be
    # either class's, so it is held to the plan and never rewritten.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {**TWO_ROOTS, user: _ROOT_USER})

    try:
        report = rename(
            root,
            _missing(store, user),
            PROJECT,
            target,
            "fetch",
            allow_heuristic=True,
            dry_run=True,
        )
        unplanned = list(report.unplanned)
    except RenameRefused as refused:
        unplanned = refused.unplanned

    assert [(s.kind, s.path, s.line, s.col, s.resolution) for s in unplanned] == [
        ("call", user, 6, 13, "receiver_unknown")
    ]


@pytest.mark.parametrize(
    ("files", "user", "text"),
    [
        # Only the repository's root holds `pkg/cache.py`.
        (
            {"pkg/__init__.py": "", "pkg/cache.py": _CACHE_CLASS, "src/x.py": ""},
            "app/use.py",
            _ROOT_USER,
        ),
        # A relative import names one module whatever the roots.
        (
            TWO_ROOTS,
            "pkg/use.py",
            _ROOT_USER.replace("from pkg.cache", "from .cache"),
        ),
    ],
    ids=["one-root", "relative"],
)
def test_an_import_one_module_satisfies_is_still_rewritten(
    tmp_path: Path, files: dict[str, str], user: str, text: str
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**files, user: text})

    report = rename(
        root,
        _missing(store, user),
        PROJECT,
        f"{PROJECT}.pkg.cache.Cache.get",
        "fetch",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col, s.resolution) for s in report.unplanned] == [
        (user, 6, 13, "unplanned")
    ]
    assert "return c.fetch(key)" in (root / user).read_text()


CLASSIC = {
    "web/lib.js": "function helper(a) {\n  return a;\n}\n",
    "web/page.js": "function run() {\n  return helper(1);\n}\n",
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
def test_a_classic_scripts_global_function_is_held_in_every_script(
    tmp_path: Path, allow_heuristic: bool
) -> None:
    # Review of PR #2797: a classic script (no import or export) puts its
    # top-level `helper` on the page's global object, and another script
    # calls it by its bare name. It may be the function, so it is held to
    # the plan and never rewritten on a guess.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, CLASSIC)
    before = _tree(root)

    graph = _missing(store, "web/page.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.web.lib.helper",
            "assist",
            allow_heuristic=allow_heuristic,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "web/page.js", 2, 9, "receiver_unknown")]
    assert _tree(root) == before


# What a classic script may say about modules without being one: prose and
# data, never code.
CLASSIC_MENTIONS = {
    "comment-require": "// usage: require('x')\n",
    "string-module-exports": 'const note = "module.exports";\n',
    "template-exports": "const note = `exports.f and require('y')`;\n",
}


@pytest.mark.parametrize("mention", sorted(CLASSIC_MENTIONS))
def test_a_module_marker_in_prose_leaves_the_script_classic(
    tmp_path: Path, mention: str
) -> None:
    # Review of PR #2797: `require(` in a comment or `module.exports` in a
    # string does not make the defining file a CommonJS module, so its
    # `helper` is still a global another script calls bare.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**CLASSIC, "web/lib.js": CLASSIC_MENTIONS[mention] + CLASSIC["web/lib.js"]}
    store, _updater = _indexed(root, files)
    before = _tree(root)

    graph = _missing(store, "web/page.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.web.lib.helper",
            "assist",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "web/page.js", 2, 9, "receiver_unknown")]
    assert _tree(root) == before


COMMONJS_CODE = {
    "require-call": "const fs = require('fs');\n",
    "module-exports": "module.exports.version = 1;\n",
    "exports-member": "exports.version = 1;\n",
}


@pytest.mark.parametrize("code", sorted(COMMONJS_CODE))
def test_commonjs_code_makes_the_file_a_module(tmp_path: Path, code: str) -> None:
    # A real `require()`, `module.exports` or `exports.x` makes it a Node
    # module, whose `helper` no other file reaches by its bare name.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {**CLASSIC, "web/lib.js": COMMONJS_CODE[code] + CLASSIC["web/lib.js"]}
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _missing(store, "web/page.js"),
        PROJECT,
        f"{PROJECT}.web.lib.helper",
        "assist",
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "web/page.js").read_text() == CLASSIC["web/page.js"]


# --- review of PR #2797, seventh round --------------------------------------


def _without_imports(store: _StatefulIngestor, *paths: str) -> QueryFn:
    """The graph as an indexer that recorded no import statement in `paths`,
    though it kept their calls."""

    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = store.fetch_all(query, None if params is None else dict(params))
        if query == cq.CYPHER_GRAPH_IMPORTERS:
            return [row for row in rows if row.get(cs.KEY_PATH) not in paths]
        return rows

    return fetch_all


# An importer of the function, and where its import names it.
UNRECORDED_IMPORTS = {
    "python": (PY, PROJECT, HELPER, "pkg/app.py", (1, 21)),
    "python-alias": (
        {
            **PY,
            "pkg/app.py": (
                "from pkg.util import helper as h\n\n\ndef run():\n    return h(1, 2)\n"
            ),
        },
        PROJECT,
        HELPER,
        "pkg/app.py",
        (1, 21),
    ),
    "javascript": (JS, PROJECT, f"{PROJECT}.src.util.helper", "src/app.js", (1, 9)),
    "rust": (
        {
            "Cargo.toml": RUST["Cargo.toml"],
            "src/lib.rs": "mod util;\nmod app;\n",
            "src/util.rs": "pub fn helper() -> i32 {\n    1\n}\n",
            "src/app.rs": (
                "use crate::util::helper;\n\npub fn go() -> i32 {\n    helper()\n}\n"
            ),
        },
        "mr",
        "mr.src.util.helper",
        "src/app.rs",
        (1, 17),
    ),
}


@pytest.mark.parametrize("case", sorted(UNRECORDED_IMPORTS))
def test_an_import_the_graph_did_not_record_is_held_to_the_plan(
    tmp_path: Path, case: str
) -> None:
    # Review of PR #2797: with the IMPORTS row missing and the call known,
    # renaming the definition and the call alone would leave the import
    # naming a function that no longer exists. The import's own token is
    # read like any other, so it is listed.
    files, project, qn, importer, (line, col) = UNRECORDED_IMPORTS[case]
    root = tmp_path / project
    root.mkdir()
    store, _updater = _indexed(root, files, project=project)

    graph = _without_imports(store, importer)
    with pytest.raises(RenameRefused) as refused:
        rename(root, graph, project, qn, "assist", dry_run=True)

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("reference", importer, line, col, "unplanned")]


def test_allow_heuristic_rewrites_an_import_the_graph_did_not_record(
    py_repo: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, _updater = py_repo

    report = rename(
        root,
        _without_imports(store, "pkg/app.py"),
        PROJECT,
        HELPER,
        "assist",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert (root / "pkg/app.py").read_text() == (
        "from pkg.util import assist\n\n\ndef run():\n    return assist(1, 2)\n"
    )


# A classic script's own `exports`, `module` or `require`: a local of a
# function, not the Node module's.
LOCAL_COMMONJS_NAMES = {
    "const-exports": (
        "function make() {\n  const exports = {};\n  exports.x = 1;\n  return exports;\n}\n"
    ),
    "exports-parameter": "function prepare(exports) {\n  exports = {};\n}\n",
    "require-parameter": "function load(require) {\n  return require('x');\n}\n",
    "local-module": (
        "function wrap() {\n  const module = { exports: {} };\n"
        "  module.exports.x = 1;\n  return module;\n}\n"
    ),
}


@pytest.mark.parametrize("local", sorted(LOCAL_COMMONJS_NAMES))
def test_a_local_named_like_commonjs_leaves_the_script_classic(
    tmp_path: Path, local: str
) -> None:
    # Review of PR #2797: `exports` declared or taken as a parameter in a
    # function is that function's, so the file is still a classic script and
    # its `helper` a global another script calls bare.
    root = tmp_path / PROJECT
    root.mkdir()
    lib = CLASSIC["web/lib.js"] + "\n" + LOCAL_COMMONJS_NAMES[local]
    store, _updater = _indexed(root, {**CLASSIC, "web/lib.js": lib})
    before = _tree(root)

    graph = _missing(store, "web/page.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.web.lib.helper",
            "assist",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "web/page.js", 2, 9, "receiver_unknown")]
    assert _tree(root) == before


FREE_COMMONJS_CODE = {
    "require-in-a-function": "function load() {\n  return require('fs');\n}\n",
    "module-exports-assigned": "module.exports = {};\n",
}


@pytest.mark.parametrize("code", sorted(FREE_COMMONJS_CODE))
def test_free_commonjs_names_still_make_the_file_a_module(
    tmp_path: Path, code: str
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    lib = CLASSIC["web/lib.js"] + "\n" + FREE_COMMONJS_CODE[code]
    store, _updater = _indexed(root, {**CLASSIC, "web/lib.js": lib})

    report = rename(
        root,
        _missing(store, "web/page.js"),
        PROJECT,
        f"{PROJECT}.web.lib.helper",
        "assist",
    )

    assert report.applied, report.message
    assert report.unplanned == ()


# --- review of PR #2797, eighth round ---------------------------------------

# A free `require('fs')` beside a `require` declared where it cannot reach.
UNREACHING_REQUIRES = {
    "sibling-block": (
        "const fs = require('fs');\n\nif (fs) {\n  const require = null;\n}\n"
    ),
    "other-function": (
        "const fs = require('fs');\n\n"
        "function other() {\n  const require = null;\n  return require;\n}\n"
    ),
    "parameter-elsewhere": (
        "function load(require) {\n  return require('x');\n}\n\n"
        "const fs = require('fs');\n"
    ),
}


@pytest.mark.parametrize("code", sorted(UNREACHING_REQUIRES))
def test_a_require_declared_elsewhere_leaves_the_free_call_commonjs(
    tmp_path: Path, code: str
) -> None:
    # Review of PR #2797: `const require` in a block, or a function's own
    # `require`, binds only there; the top-level `require('fs')` is Node's,
    # so the file is a module and no other file reaches its `helper` bare.
    root = tmp_path / PROJECT
    root.mkdir()
    lib = UNREACHING_REQUIRES[code] + "\n" + CLASSIC["web/lib.js"]
    store, _updater = _indexed(root, {**CLASSIC, "web/lib.js": lib})

    report = rename(
        root,
        _missing(store, "web/page.js"),
        PROJECT,
        f"{PROJECT}.web.lib.helper",
        "assist",
        allow_heuristic=True,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "web/page.js").read_text() == CLASSIC["web/page.js"]


# A `require` every call of which a declaration around it shadows.
SHADOWED_REQUIRES = {
    "same-block": "if (true) {\n  const require = (x) => x;\n  require('x');\n}\n",
    "top-level-const": "const require = (x) => x;\n\nrequire('x');\n",
    "enclosing-function": (
        "function load() {\n  const require = (x) => x;\n"
        "  return () => require('x');\n}\n"
    ),
}


@pytest.mark.parametrize("code", sorted(SHADOWED_REQUIRES))
def test_a_require_shadowed_around_the_call_leaves_the_script_classic(
    tmp_path: Path, code: str
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    lib = CLASSIC["web/lib.js"] + "\n" + SHADOWED_REQUIRES[code]
    store, _updater = _indexed(root, {**CLASSIC, "web/lib.js": lib})

    graph = _missing(store, "web/page.js")
    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.web.lib.helper",
            "assist",
            allow_heuristic=True,
        )

    assert [
        (s.kind, s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned
    ] == [("call", "web/page.js", 2, 9, "receiver_unknown")]


# --- review of PR #2797, ninth round ----------------------------------------

# A `.pyi` stub declares its module's interface (#2445). The indexer skips a
# stub whose `.py` it parses, so the graph has no site in it.
STUBBED = {
    "pkg/__init__.py": "",
    "pkg/widget.py": (
        "class Widget:\n"
        "    def spin(self):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def make():\n"
        "    return Widget()\n"
    ),
    "pkg/widget.pyi": (
        '__all__ = ["Widget", "make"]\n'
        "\n"
        "class Widget:\n"
        "    def spin(self) -> int: ...\n"
        "\n"
        "peer: Widget\n"
        "\n"
        "def make() -> Widget: ...\n"
    ),
}
# Each target, its new name and where the stub spells it: the declaration,
# then the uses.
STUB_TARGETS = {
    "class": (f"{PROJECT}.pkg.widget.Widget", "Gadget", [(3, 6), (6, 6), (8, 14)]),
    "function": (f"{PROJECT}.pkg.widget.make", "build", [(8, 4)]),
    "method": (f"{PROJECT}.pkg.widget.Widget.spin", "turn", [(4, 8)]),
}


@pytest.mark.parametrize("kind", sorted(STUB_TARGETS))
def test_a_companion_stub_refuses_the_rename_by_default(
    tmp_path: Path, kind: str
) -> None:
    # Review of PR #2797 (Greptile): renaming `Widget` rewrote `widget.py`
    # and reported success while `widget.pyi` still declared `class Widget`
    # and annotated `peer: Widget`, so the module and its interface
    # disagreed. The stub's own `class Widget` hid every use in it, as
    # another module's same-named class would.
    qn, new_name, positions = STUB_TARGETS[kind]
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, STUBBED)
    before = _tree(root)

    with pytest.raises(RenameRefused) as refused:
        rename(root, _query(store), PROJECT, qn, new_name)

    assert [(s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned] == [
        ("pkg/widget.pyi", line, col, "unplanned") for line, col in positions
    ]
    assert _tree(root) == before


@pytest.mark.parametrize("kind", sorted(STUB_TARGETS))
def test_allow_heuristic_renames_the_companion_stub_with_its_module(
    tmp_path: Path, kind: str
) -> None:
    qn, new_name, positions = STUB_TARGETS[kind]
    old_name = qn.rsplit(".", 1)[-1]
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, STUBBED)

    report = rename(
        root,
        _query(store),
        PROJECT,
        qn,
        new_name,
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col) for s in report.unplanned] == [
        ("pkg/widget.pyi", line, col) for line, col in positions
    ]
    # The stub's `__all__` follows the module's own.
    for path in ("pkg/widget.py", "pkg/widget.pyi"):
        assert (root / path).read_text() == STUBBED[path].replace(old_name, new_name)


# Where a stub sits beside the module it declares, as the indexer pairs
# them: `x.pyi` for `x.py`, `__init__.pyi` for the `__init__.py` beside it,
# and `x.pyi` for the package `x/`.
STUB_COMPANIONS = {
    "module": ("pkg/util.py", "pkg/util.pyi", HELPER),
    "package-init": ("pkg/__init__.py", "pkg/__init__.pyi", f"{PROJECT}.pkg.helper"),
    "package-beside": ("pkg/__init__.py", "pkg.pyi", f"{PROJECT}.pkg.helper"),
}


@pytest.mark.parametrize("where", sorted(STUB_COMPANIONS))
def test_a_stub_is_held_to_the_module_it_declares(tmp_path: Path, where: str) -> None:
    implementation, stub, qn = STUB_COMPANIONS[where]
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "pkg/__init__.py": "",
        implementation: "def helper(a, b):\n    return a + b\n",
        stub: "def helper(a: int, b: int) -> int: ...\n",
    }
    store, _updater = _indexed(root, files)

    with pytest.raises(RenameRefused) as refused:
        rename(root, _query(store), PROJECT, qn, "assist", dry_run=True)

    assert [(s.path, s.line, s.col) for s in refused.value.unplanned] == [(stub, 1, 4)]


UNRELATED_STUB = "class Widget: ...\n\npeer: Widget\n\ndef make() -> Widget: ...\n"
# Another module's stub with a `Widget` of its own: beside its `.py`, or as
# the module itself.
UNRELATED_STUBS = {
    "stubbed-module": {
        "other/__init__.py": "",
        "other/widget.py": "class Widget:\n    pass\n",
        "other/widget.pyi": UNRELATED_STUB,
    },
    "stub-only-module": {
        "other/__init__.py": "",
        "other/widget.pyi": UNRELATED_STUB,
    },
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize("case", sorted(UNRELATED_STUBS))
def test_another_modules_stub_is_neither_refused_nor_rewritten(
    tmp_path: Path, case: str, allow_heuristic: bool
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    own = {path: STUBBED[path] for path in ("pkg/__init__.py", "pkg/widget.py")}
    store, updater = _indexed(root, {**own, **UNRELATED_STUBS[case]})

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.pkg.widget.Widget",
        "Gadget",
        allow_heuristic=allow_heuristic,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "other/widget.pyi").read_text() == UNRELATED_STUB


@pytest.mark.parametrize("kind", sorted(STUB_TARGETS))
def test_a_module_with_no_stub_renames_as_before(tmp_path: Path, kind: str) -> None:
    qn, new_name, _positions = STUB_TARGETS[kind]
    root = tmp_path / PROJECT
    root.mkdir()
    own = {path: STUBBED[path] for path in ("pkg/__init__.py", "pkg/widget.py")}
    store, updater = _indexed(root, own)

    report = rename(
        root, _query(store), PROJECT, qn, new_name, reingest=updater.reingest
    )

    assert report.applied, report.message
    assert report.unplanned == ()


def test_a_stub_only_module_is_its_own_definition(tmp_path: Path) -> None:
    # With no `.py` beside it the stub is the module: the graph has its
    # definition and sites, and the cross-check reads it as any module.
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "pkg/__init__.py": "",
        "pkg/widget.pyi": "def make() -> int: ...\n",
        "pkg/app.py": "from pkg.widget import make\n\n\ndef run():\n    return make()\n",
    }
    store, updater = _indexed(root, files)

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.pkg.widget.make",
        "build",
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    for path in ("pkg/widget.pyi", "pkg/app.py"):
        assert (root / path).read_text() == files[path].replace("make", "build")


@pytest.mark.parametrize(
    ("language", "source", "access"),
    [
        (cs.SupportedLanguage.PYTHON, b"def make() -> Widget: ...", _Access.BARE),
        (cs.SupportedLanguage.RUST, b"fn new() -> Widget {", _Access.BARE),
        (cs.SupportedLanguage.PHP, b"$this->Widget", _Access.MEMBER),
        (cs.SupportedLanguage.CPP, b"self->Widget", _Access.MEMBER),
        (cs.SupportedLanguage.PYTHON, b"pkg.Widget", _Access.MEMBER),
    ],
)
def test_an_arrow_reaches_a_member_only_where_the_language_says_so(
    language: cs.SupportedLanguage, source: bytes, access: _Access
) -> None:
    # A return annotation is not a member access: `def make() -> Widget`
    # in another module's stub is that module's `Widget`, hidden by its own.
    assert _access(source, source.index(b"Widget"), language) is access


# --- review of PR #2797: crate names and tsconfig aliases -------------------

CRATE_MANIFEST = '[package]\nname = "mini-redis"\nversion = "0.1.0"\nedition = "2021"\n'
CRATE = {
    "src/util.rs": "pub fn helper() -> i32 {\n    1\n}\n",
    "Cargo.toml": CRATE_MANIFEST,
    "src/lib.rs": "pub mod util;\n",
}
# Files outside the library that reach `helper` by the crate's name, as
# Cargo builds them, with the lines that name it.
CRATE_USERS = {
    "test-use": (
        "tests/it.rs",
        "use mini_redis::util::helper;\n\nfn go() -> i32 {\n    helper()\n}\n",
        [1, 4],
    ),
    "example-module": (
        "examples/demo.rs",
        "use mini_redis::util;\n\nfn go() -> i32 {\n    util::helper()\n}\n",
        [4],
    ),
    "bin-path": (
        "src/bin/cli.rs",
        "fn go() -> i32 {\n    mini_redis::util::helper()\n}\n",
        [2],
    ),
}


def _user_occurrences(
    root: Path,
    files: dict[str, str],
    user: str,
    name: str,
    language: cs.SupportedLanguage,
) -> list[tuple[str, int, bool]]:
    return [
        found
        for found in _function_occurrences(root, files, name, language)
        if found[0] == user
    ]


@pytest.mark.parametrize("user", sorted(CRATE_USERS))
def test_a_rust_use_through_the_crates_own_name_is_the_function(
    tmp_path: Path, user: str
) -> None:
    # Review of PR #2797 (CodeRabbit): only `crate`, `self` and `super`
    # rooted a path, so `use mini_redis::util::helper;` in `tests/` named no
    # module, and both the use and the call were dropped.
    path, text, lines = CRATE_USERS[user]

    found = _user_occurrences(
        tmp_path, {**CRATE, path: text}, path, "helper", cs.SupportedLanguage.RUST
    )

    assert found == [(path, line, True) for line in lines]


def test_a_lib_name_in_the_manifest_is_the_name_code_imports(tmp_path: Path) -> None:
    manifest = CRATE_MANIFEST + '\n[lib]\nname = "redis_core"\n'
    user = "use redis_core::util::helper;\n\nfn go() -> i32 {\n    helper()\n}\n"
    files = {**CRATE, "Cargo.toml": manifest, "tests/it.rs": user}

    found = _user_occurrences(
        tmp_path, files, "tests/it.rs", "helper", cs.SupportedLanguage.RUST
    )

    assert found == [("tests/it.rs", 1, True), ("tests/it.rs", 4, True)]


# A crate-name path the project cannot resolve: another crate, a manifest
# that is missing or unreadable. Each is read as before.
UNRESOLVED_CRATES = {
    "other-crate": (CRATE, "use other_crate::util::helper;\n"),
    "no-manifest": (
        {path: text for path, text in CRATE.items() if path != "Cargo.toml"},
        "use mini_redis::util::helper;\n",
    ),
    "unreadable-manifest": (
        {**CRATE, "Cargo.toml": "[package\nname = "},
        "use mini_redis::util::helper;\n",
    ),
}


@pytest.mark.parametrize("case", sorted(UNRESOLVED_CRATES))
def test_a_crate_name_the_project_does_not_give_is_not_the_function(
    tmp_path: Path, case: str
) -> None:
    files, head = UNRESOLVED_CRATES[case]
    user = f"{head}\nfn go() -> i32 {{\n    helper()\n}}\n"

    found = _user_occurrences(
        tmp_path,
        {**files, "tests/it.rs": user},
        "tests/it.rs",
        "helper",
        cs.SupportedLanguage.RUST,
    )

    assert found == []


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
def test_a_missed_call_through_the_crate_name_refuses_the_rename(
    tmp_path: Path, allow_heuristic: bool
) -> None:
    path, text, _lines = CRATE_USERS["test-use"]
    root = tmp_path / "mr"
    root.mkdir()
    store, _updater = _indexed(root, {**CRATE, path: text}, project="mr")

    graph = _missing(store, path)
    try:
        report = rename(
            root,
            graph,
            "mr",
            "mr.src.util.helper",
            "assist",
            allow_heuristic=allow_heuristic,
            dry_run=True,
        )
    except RenameRefused as refused:
        assert not allow_heuristic
        assert ("call", path, 4, 4) in {
            (s.kind, s.path, s.line, s.col) for s in refused.unplanned
        }
        return
    assert allow_heuristic
    assert ("call", path, 4, 4, "unplanned") in {
        (s.kind, s.path, s.line, s.col, s.resolution) for s in report.unplanned
    }


TS_HELPER = "export function helper(): number {\n  return 1;\n}\n"
TS_USER = "import { helper } from '%s';\n\nexport function go(): number {\n  return helper();\n}\n"
# tsconfig is JSONC: a comment and trailing commas.
TS_ALIAS_CONFIG = (
    "{\n"
    "  // the app's aliases\n"
    '  "compilerOptions": {\n'
    '    "baseUrl": ".",\n'
    '    "paths": {\n'
    '      "@/*": ["src/*"],\n'
    "    },\n"
    "  },\n"
    "}\n"
)
# A config and the specifier that reaches `src/utils/helper.ts` through it.
TS_CONFIGS = {
    "paths-alias": ("tsconfig.json", TS_ALIAS_CONFIG, "@/utils/helper"),
    "jsconfig-alias": ("jsconfig.json", TS_ALIAS_CONFIG, "@/utils/helper"),
    "base-url": (
        "tsconfig.json",
        '{"compilerOptions": {"baseUrl": "src"}}\n',
        "utils/helper",
    ),
}


@pytest.mark.parametrize("config", sorted(TS_CONFIGS))
def test_a_tsconfig_alias_import_is_the_function(tmp_path: Path, config: str) -> None:
    # Review of PR #2797 (CodeRabbit): `@/utils/helper` was read as a package
    # path, which names no file of the project, so the import and the call
    # were dropped.
    name, text, specifier = TS_CONFIGS[config]
    files = {
        "src/utils/helper.ts": TS_HELPER,
        name: text,
        "src/app.ts": TS_USER % specifier,
    }

    found = _user_occurrences(
        tmp_path, files, "src/app.ts", "helper", cs.SupportedLanguage.TS
    )

    assert found == [("src/app.ts", 1, True), ("src/app.ts", 4, True)]


def test_an_alias_the_nearest_config_cannot_resolve_is_held_as_uncertain(
    tmp_path: Path,
) -> None:
    # `web/tsconfig.json` extends the root's, which `paths` are not read
    # through: `@/utils/helper` may be the function, so it is held to the
    # plan, never rewritten.
    files = {
        "src/utils/helper.ts": TS_HELPER,
        "tsconfig.json": TS_ALIAS_CONFIG,
        "web/tsconfig.json": '{"extends": "../tsconfig.json"}\n',
        "web/app.ts": TS_USER % "@/utils/helper",
    }

    found = _user_occurrences(
        tmp_path, files, "web/app.ts", "helper", cs.SupportedLanguage.TS
    )

    assert found == [("web/app.ts", 1, False), ("web/app.ts", 4, False)]


# A non-relative specifier that is not the project's own, or a project
# with no config to resolve one: read as before.
EXTERNAL_SPECIFIERS = {
    "npm-package": (TS_ALIAS_CONFIG, "lodash"),
    "scoped-package": (TS_ALIAS_CONFIG, "@scope/utils/helper"),
    "no-config": (None, "@/utils/helper"),
    "unreadable-config": ('{"compilerOptions": {', "@/utils/helper"),
}


@pytest.mark.parametrize("case", sorted(EXTERNAL_SPECIFIERS))
def test_a_package_import_is_not_the_function(tmp_path: Path, case: str) -> None:
    config, specifier = EXTERNAL_SPECIFIERS[case]
    files = {
        "src/utils/helper.ts": TS_HELPER,
        "src/app.ts": TS_USER % specifier,
        **({} if config is None else {"tsconfig.json": config}),
    }

    found = _user_occurrences(
        tmp_path, files, "src/app.ts", "helper", cs.SupportedLanguage.TS
    )

    assert found == []


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
def test_a_missed_call_through_a_tsconfig_alias_refuses_the_rename(
    tmp_path: Path, allow_heuristic: bool
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "src/utils/helper.ts": TS_HELPER,
        "tsconfig.json": TS_ALIAS_CONFIG,
        "src/app.ts": TS_USER % "@/utils/helper",
    }
    store, _updater = _indexed(root, files)

    graph = _missing(store, "src/app.ts")
    try:
        report = rename(
            root,
            graph,
            PROJECT,
            f"{PROJECT}.src.utils.helper.helper",
            "assist",
            allow_heuristic=allow_heuristic,
            dry_run=True,
        )
    except RenameRefused as refused:
        assert not allow_heuristic
        assert ("call", "src/app.ts", 4, 9) in {
            (s.kind, s.path, s.line, s.col) for s in refused.unplanned
        }
        return
    assert allow_heuristic
    assert ("call", "src/app.ts", 4, 9, "unplanned") in {
        (s.kind, s.path, s.line, s.col, s.resolution) for s in report.unplanned
    }


@pytest.mark.parametrize("case", ["npm-package", "no-config"])
def test_an_external_import_does_not_refuse_the_rename(
    tmp_path: Path, case: str
) -> None:
    config, specifier = EXTERNAL_SPECIFIERS[case]
    root = tmp_path / PROJECT
    root.mkdir()
    files = {
        "src/utils/helper.ts": TS_HELPER,
        "src/app.ts": TS_USER % specifier,
        **({} if config is None else {"tsconfig.json": config}),
    }
    store, _updater = _indexed(root, files)

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.src.utils.helper.helper",
        "assist",
        dry_run=True,
    )

    assert report.unplanned == ()
    assert "src/app.ts" not in {s.path for s in report.sites}


# --- review of PR #2797: the re-export alias idiom --------------------------

ALIASED = {
    "pkg/__init__.py": "from .widget import Widget, helper\n",
    "pkg/widget.py": "class Widget:\n    pass\n\n\ndef helper():\n    return 1\n",
}
# A file the graph has no site in, importing the target under its own name
# or another, with the positions that name the target: the imported name,
# then the alias and its uses where the alias spells the target.
ALIAS_IMPORTERS = {
    "stub-class": (
        "pkg/__init__.pyi",
        "from .widget import Widget as Widget\n\nDEFAULT: Widget\n",
        f"{PROJECT}.pkg.widget.Widget",
        [(1, 20), (1, 30), (3, 9)],
    ),
    "stub-function": (
        "pkg/__init__.pyi",
        "from .widget import helper as helper\n\nDEFAULT: int = helper()\n",
        f"{PROJECT}.pkg.widget.helper",
        [(1, 20), (1, 30), (3, 15)],
    ),
    "module-class": (
        "app.py",
        "from pkg.widget import Widget as Widget\n\n\ndef build():\n"
        "    return Widget()\n",
        f"{PROJECT}.pkg.widget.Widget",
        [(1, 23), (1, 33), (5, 11)],
    ),
    "other-name": (
        "pkg/__init__.pyi",
        "from .widget import Widget as W\n\nDEFAULT: W\n",
        f"{PROJECT}.pkg.widget.Widget",
        [(1, 20)],
    ),
}


def _unseen(store: _StatefulIngestor, *paths: str) -> QueryFn:
    """The graph as an indexer that recorded nothing in `paths`: no call,
    reference, type edge or import."""
    calls = _missing(store, *paths)

    def fetch_all(query: str, params: PropertyParams | None) -> list[ResultRow]:
        rows = calls(query, params)
        if query in (cq.CYPHER_GRAPH_IMPORTERS, cq.CYPHER_GRAPH_TYPE_EDGES):
            return [row for row in rows if row.get(cs.KEY_PATH) not in paths]
        return rows

    return fetch_all


@pytest.mark.parametrize("case", sorted(ALIAS_IMPORTERS))
def test_an_import_of_the_target_under_its_own_name_is_held_to_the_plan(
    tmp_path: Path, case: str
) -> None:
    # Review of PR #2797: `from .widget import Widget as Widget` binds
    # `Widget` in the file, and that binding hid the import and every use
    # behind it as another symbol's, so the rename reported success and left
    # them all under the old name.
    path, text, qn, positions = ALIAS_IMPORTERS[case]
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {**ALIASED, path: text})

    graph = _unseen(store, path)
    with pytest.raises(RenameRefused) as refused:
        rename(root, graph, PROJECT, qn, "Renamed", dry_run=True)

    assert [(s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned] == [
        (path, line, col, "unplanned") for line, col in positions
    ]


@pytest.mark.parametrize("case", sorted(ALIAS_IMPORTERS))
def test_allow_heuristic_renames_an_import_under_the_targets_own_name(
    tmp_path: Path, case: str
) -> None:
    path, text, qn, positions = ALIAS_IMPORTERS[case]
    old_name = qn.rsplit(".", 1)[-1]
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, {**ALIASED, path: text})

    report = rename(
        root,
        _unseen(store, path),
        PROJECT,
        qn,
        "Renamed",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col) for s in report.unplanned] == [
        (path, line, col) for line, col in positions
    ]
    assert (root / path).read_text() == text.replace(old_name, "Renamed")


def test_an_import_under_the_targets_name_from_another_module_is_not_held(
    tmp_path: Path,
) -> None:
    # `other` has a `Widget` of its own; its stub re-exports that one.
    root = tmp_path / PROJECT
    root.mkdir()
    other_stub = "from .widget import Widget as Widget\n\nDEFAULT: Widget\n"
    files = {
        **ALIASED,
        "other/__init__.py": "from .widget import Widget\n",
        "other/__init__.pyi": other_stub,
        "other/widget.py": "class Widget:\n    pass\n",
    }
    store, updater = _indexed(root, files)

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.pkg.widget.Widget",
        "Gadget",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "other/__init__.pyi").read_text() == other_stub


def test_a_module_imported_under_the_targets_name_is_not_the_target(
    tmp_path: Path,
) -> None:
    # `import pkg.widget as Widget` binds the module, not the class: the
    # file's `Widget` is that module, as before.
    root = tmp_path / PROJECT
    root.mkdir()
    user = "import pkg.widget as Widget\n\nDEFAULT = Widget\n"
    store, _updater = _indexed(root, {**ALIASED, "app.py": user})

    report = rename(
        root,
        _unseen(store, "app.py"),
        PROJECT,
        f"{PROJECT}.pkg.widget.Widget",
        "Gadget",
        dry_run=True,
    )

    assert report.unplanned == ()


# --- review of PR #2797: a `.d.ts` beside its module ------------------------

TS_UTIL = "export function helper(): number {\n  return 1;\n}\n\nexport class Widget {\n  spin(): number {\n    return 1;\n  }\n}\n"
TS_DECLARATIONS = (
    "export declare function helper(): number;\n"
    "export declare class Widget {\n"
    "  spin(): number;\n"
    "}\n"
    "export declare const make: typeof helper;\n"
)
# Each target, its new name, and where the `.d.ts` spells it.
DECLARED_TARGETS = {
    "function": (f"{PROJECT}.src.util.helper", "assist", [(1, 24), (5, 34)]),
    "class": (f"{PROJECT}.src.util.Widget", "Gadget", [(2, 21)]),
    "method": (f"{PROJECT}.src.util.Widget.spin", "turn", [(3, 2)]),
}
# The module the `.d.ts` declares, in each language it may be written in.
DECLARED_MODULES = {
    "ts": "src/util.ts",
    "js": "src/util.js",
}


def _declared_module(language: str) -> dict[str, str]:
    path = DECLARED_MODULES[language]
    text = TS_UTIL if language == "ts" else TS_UTIL.replace("(): number", "()")
    return {path: text, "src/util.d.ts": TS_DECLARATIONS}


@pytest.mark.parametrize("language", sorted(DECLARED_MODULES))
@pytest.mark.parametrize("kind", sorted(DECLARED_TARGETS))
def test_a_declaration_file_beside_the_module_refuses_by_default(
    tmp_path: Path, kind: str, language: str
) -> None:
    # Review of PR #2797: the graph indexes `util.d.ts` beside `util.ts` as
    # a module of its own, so its `helper` was credited to that symbol and
    # the rename left the declaration under the old name.
    qn, new_name, positions = DECLARED_TARGETS[kind]
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, _declared_module(language))

    with pytest.raises(RenameRefused) as refused:
        rename(root, _query(store), PROJECT, qn, new_name, dry_run=True)

    assert [(s.path, s.line, s.col, s.resolution) for s in refused.value.unplanned] == [
        ("src/util.d.ts", line, col, "unplanned") for line, col in positions
    ]


@pytest.mark.parametrize("language", sorted(DECLARED_MODULES))
@pytest.mark.parametrize("kind", sorted(DECLARED_TARGETS))
def test_allow_heuristic_renames_the_declaration_file_with_its_module(
    tmp_path: Path, kind: str, language: str
) -> None:
    qn, new_name, positions = DECLARED_TARGETS[kind]
    old_name = qn.rsplit(".", 1)[-1]
    root = tmp_path / PROJECT
    root.mkdir()
    files = _declared_module(language)
    store, updater = _indexed(root, files)

    report = rename(
        root,
        _query(store),
        PROJECT,
        qn,
        new_name,
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert [(s.path, s.line, s.col) for s in report.unplanned] == [
        ("src/util.d.ts", line, col) for line, col in positions
    ]
    lines = files["src/util.d.ts"].splitlines(keepends=True)
    for line, col in positions:
        text = lines[line - 1]
        lines[line - 1] = text[:col] + new_name + text[col + len(old_name) :]
    assert (root / "src/util.d.ts").read_text() == "".join(lines)


# Declaration files that are not the target's: another module's, beside its
# own `.ts` or alone, and the target's module declared only by a `.d.ts`.
OTHER_DECLARATIONS = {
    "other-module": {
        "src/util.ts": TS_UTIL,
        "src/other.ts": TS_UTIL,
        "src/other.d.ts": TS_DECLARATIONS,
    },
    "other-declaration-only": {
        "src/util.ts": TS_UTIL,
        "src/other.d.ts": TS_DECLARATIONS,
    },
}


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize("case", sorted(OTHER_DECLARATIONS))
def test_another_modules_declaration_file_is_neither_refused_nor_rewritten(
    tmp_path: Path, case: str, allow_heuristic: bool
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    store, updater = _indexed(root, OTHER_DECLARATIONS[case])

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.src.util.helper",
        "assist",
        allow_heuristic=allow_heuristic,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "src/other.d.ts").read_text() == TS_DECLARATIONS


def test_a_declaration_file_alone_is_its_own_module(tmp_path: Path) -> None:
    # With no `.ts` beside it the `.d.ts` is the module: its own `typeof
    # helper` is a use the graph has no site for, as before.
    root = tmp_path / PROJECT
    root.mkdir()
    store, _updater = _indexed(root, {"src/util.d.ts": TS_DECLARATIONS})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _query(store),
            PROJECT,
            f"{PROJECT}.src.util.helper",
            "assist",
            dry_run=True,
        )

    assert [(s.path, s.line, s.col) for s in refused.value.unplanned] == [
        ("src/util.d.ts", 5, 34)
    ]


def test_a_same_named_declaration_nested_elsewhere_is_not_the_targets(
    tmp_path: Path,
) -> None:
    # `NS.helper` in the `.d.ts` is not the module's own `helper`.
    root = tmp_path / PROJECT
    root.mkdir()
    nested = "export declare namespace NS {\n  function helper(): number;\n}\n"
    files = {"src/util.ts": TS_UTIL, "src/util.d.ts": nested}
    store, updater = _indexed(root, files)

    report = rename(
        root,
        _query(store),
        PROJECT,
        f"{PROJECT}.src.util.helper",
        "assist",
        allow_heuristic=True,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "src/util.d.ts").read_text() == nested


# --- review of PR #2797: a class another module's import binds --------------

PY_CLASS_BODY = (
    "\n\n\ndef build(x: Widget) -> Widget:\n"
    "    if isinstance(x, Widget):\n"
    "        return x\n"
    "    return Widget()\n"
    "\n\n"
    "class Sub(Widget):\n"
    "    pass\n"
)
TWO_WIDGETS = {
    "pkg/__init__.py": "",
    "pkg/widget.py": "class Widget:\n    pass\n",
    "other/__init__.py": "",
    "other/widget.py": "class Widget:\n    pass\n",
}
# `Widget` in each position of PY_CLASS_BODY after a one-line import.
PY_CLASS_USES = [(4, 13), (4, 24), (5, 21), (7, 11), (10, 10)]


# How a file reaches a module's `Widget`: imported by name, under its own
# name, or through the module (`other.widget.Widget`).
def _class_user(module: str, how: str) -> str:
    match how:
        case "import":
            return f"from {module} import Widget" + PY_CLASS_BODY
        case "alias":
            return f"from {module} import Widget as Widget" + PY_CLASS_BODY
        case _:
            return f"import {module}" + PY_CLASS_BODY.replace(
                "Widget", f"{module}.Widget"
            )


@pytest.mark.parametrize("allow_heuristic", [False, True], ids=["plain", "heuristic"])
@pytest.mark.parametrize("how", ["import", "alias", "qualified"])
def test_another_modules_class_bound_by_an_import_is_not_the_target(
    tmp_path: Path, how: str, allow_heuristic: bool
) -> None:
    # Review of PR #2797: a class counted every occurrence, so after
    # `from other.widget import Widget` the import, the annotations, the
    # `isinstance`, the call and the base class were all held to the plan,
    # and `allow_heuristic` rewrote another class's import and uses.
    root = tmp_path / PROJECT
    root.mkdir()
    user = _class_user("other.widget", how)
    store, updater = _indexed(root, {**TWO_WIDGETS, "app.py": user})

    report = rename(
        root,
        _unseen(store, "app.py"),
        PROJECT,
        f"{PROJECT}.pkg.widget.Widget",
        "Gadget",
        allow_heuristic=allow_heuristic,
        reingest=updater.reingest,
    )

    assert report.applied, report.message
    assert report.unplanned == ()
    assert (root / "app.py").read_text() == user


@pytest.mark.parametrize("how", ["import", "alias", "qualified", "wildcard"])
def test_the_targets_class_bound_by_an_import_still_counts(
    tmp_path: Path, how: str
) -> None:
    root = tmp_path / PROJECT
    root.mkdir()
    user = (
        "from pkg.widget import *" + PY_CLASS_BODY
        if how == "wildcard"
        else _class_user("pkg.widget", how)
    )
    store, _updater = _indexed(root, {**TWO_WIDGETS, "app.py": user})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _unseen(store, "app.py"),
            PROJECT,
            f"{PROJECT}.pkg.widget.Widget",
            "Gadget",
            dry_run=True,
        )

    found = {(s.line, s.resolution) for s in refused.value.unplanned}
    assert {(line, "unplanned") for line, _col in PY_CLASS_USES} <= found


def test_a_class_through_a_star_import_from_elsewhere_is_held_uncertain(
    tmp_path: Path,
) -> None:
    # `from other.widget import *` may bring in the other `Widget`: held to
    # the plan, never rewritten.
    root = tmp_path / PROJECT
    root.mkdir()
    user = "from other.widget import *" + PY_CLASS_BODY
    store, _updater = _indexed(root, {**TWO_WIDGETS, "app.py": user})

    with pytest.raises(RenameRefused) as refused:
        rename(
            root,
            _unseen(store, "app.py"),
            PROJECT,
            f"{PROJECT}.pkg.widget.Widget",
            "Gadget",
            allow_heuristic=True,
        )

    assert {(s.line, s.col) for s in refused.value.unplanned} == set(PY_CLASS_USES)
    assert {s.resolution for s in refused.value.unplanned} == {"receiver_unknown"}


def _type_occurrences(
    root: Path, files: dict[str, str], name: str, language: cs.SupportedLanguage
) -> list[tuple[str, int, int, bool]]:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    definition = next(iter(files))
    target = Target(
        name,
        language,
        cs.RenameTargetKind.TYPE,
        definition,
        frozenset(),
        frozenset({definition}),
    )
    return [
        (found.path, found.line, found.col, found.certain)
        for found in find_occurrences(root, target)
        if found.path != definition
    ]


TS_WIDGETS = {
    "src/widget.ts": "export class Widget {}\n",
    "src/other.ts": "export class Widget {}\n",
}
TS_CLASS_BODY = (
    "\n\nexport function build(x: Widget): Widget {\n"
    "  return x instanceof Widget ? x : new Widget();\n"
    "}\n\nexport class Sub extends Widget {}\n"
)
TS_CLASS_USES = [(3, 25), (3, 34), (4, 22), (4, 39), (7, 25)]
RUST_WIDGETS = {
    "src/widget.rs": "pub struct Widget;\n",
    "Cargo.toml": RUST["Cargo.toml"],
    "src/lib.rs": "pub mod widget;\npub mod other;\npub mod app;\n",
    "src/other.rs": "pub struct Widget;\n",
}
RUST_CLASS_BODY = "\n\npub fn make() -> Widget {\n    Widget\n}\n"
RUST_CLASS_USES = [(3, 17), (4, 4)]
# Each language's files, the user's path and body, how an import names
# the other `Widget` and how one names the target's, and the uses.
TYPE_IMPORTS = {
    "typescript": (
        cs.SupportedLanguage.TS,
        TS_WIDGETS,
        "src/app.ts",
        TS_CLASS_BODY,
        "import { Widget } from './other';",
        "import { Widget } from './widget';",
        TS_CLASS_USES,
    ),
    "rust": (
        cs.SupportedLanguage.RUST,
        RUST_WIDGETS,
        "src/app.rs",
        RUST_CLASS_BODY,
        "use other::Widget;",
        "use crate::widget::Widget;",
        RUST_CLASS_USES,
    ),
}


@pytest.mark.parametrize("case", sorted(TYPE_IMPORTS))
def test_another_modules_class_is_not_the_target_in_each_language(
    tmp_path: Path, case: str
) -> None:
    language, files, path, body, other, _own, _uses = TYPE_IMPORTS[case]

    found = _type_occurrences(
        tmp_path, {**files, path: other + body}, "Widget", language
    )

    assert found == []


@pytest.mark.parametrize("case", sorted(TYPE_IMPORTS))
def test_the_targets_class_still_counts_in_each_language(
    tmp_path: Path, case: str
) -> None:
    language, files, path, body, _other, own, uses = TYPE_IMPORTS[case]

    found = _type_occurrences(tmp_path, {**files, path: own + body}, "Widget", language)

    assert [(line, col, certain) for _path, line, col, certain in found] == [
        (1, own.index("Widget"), True),
        *((line, col, True) for line, col in uses),
    ]


def test_a_class_through_an_alias_the_config_cannot_resolve_is_uncertain(
    tmp_path: Path,
) -> None:
    # `web/tsconfig.json` extends the root's, whose `@/*` the reader does not
    # follow there: the import may be the target's, held, never rewritten.
    files = {
        **TS_WIDGETS,
        "tsconfig.json": TS_ALIAS_CONFIG,
        "web/tsconfig.json": '{"extends": "../tsconfig.json"}\n',
        "web/app.ts": "import { Widget } from '@/widget';" + TS_CLASS_BODY,
    }

    found = _type_occurrences(tmp_path, files, "Widget", cs.SupportedLanguage.TS)

    assert [(line, col, certain) for _path, line, col, certain in found] == [
        (1, 9, False),
        *((line, col, False) for line, col in TS_CLASS_USES),
    ]
