# Structural delta after a write (issue #1525): the touched files' subgraph
# is read before and after the scoped re-ingest and the difference reported
# as dangling callers, signature changes with per-site arity verdicts, new
# duplicates, new import cycles and the tests reaching the changed symbols.
# The graph is the in-memory stateful ingestor the reingest tests use, which
# answers the delta's fixed queries from its node and edge tables.

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.structural_delta import (
    StructuralDelta,
    has_findings,
    normalise_paths,
    observe,
    strongly_connected,
)
from evals.cgr_graph import _StatefulIngestor

PROJECT = "delta_fixture"

BIG_BODY = (
    "    total = 0\n"
    "    for item in items:\n"
    "        if item is None:\n"
    "            continue\n"
    "        if isinstance(item, str):\n"
    "            total += len(item)\n"
    "        elif item > 10:\n"
    "            total += item * 2\n"
    "        else:\n"
    "            total += item\n"
    "    return total\n"
)

FIXTURE: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/util.py": (
        "def helper(a):\n    return a + 1\n\n\ndef tally(items):\n" + BIG_BODY
    ),
    "pkg/app.py": "from pkg.util import helper\n\n\ndef run():\n    return helper(1)\n",
    "main.py": "from pkg.app import run\n\n\ndef main():\n    run()\n",
    "tests/__init__.py": "",
    "tests/test_app.py": (
        "from pkg.app import run\n\n\ndef test_run():\n    assert run() == 2\n"
    ),
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _updater(store: _StatefulIngestor, root: Path) -> GraphUpdater:
    parsers, queries = load_parsers()
    return GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
    )


@pytest.fixture
def indexed(temp_repo: Path) -> tuple[Path, _StatefulIngestor, GraphUpdater]:
    root = temp_repo / PROJECT
    root.mkdir()
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    store = _StatefulIngestor()
    updater = _updater(store, root)
    updater.run(force=True)
    return root, store, updater


def _observe(
    root: Path,
    store: _StatefulIngestor,
    updater: GraphUpdater,
    changed: list[str],
    deleted: list[str] | None = None,
) -> StructuralDelta:
    return observe(
        store.fetch_all,
        PROJECT,
        [*changed, *(deleted or [])],
        lambda: updater.reingest(changed, deleted=deleted or []),
        repo_root=root,
    )


def _qn(rel: str) -> str:
    return f"{PROJECT}.{rel}"


# --- acceptance ---------------------------------------------------------------


def test_rename_without_updating_callers_reports_dangling_callers(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def assist(a):"),
    )
    delta = _observe(root, store, updater, ["pkg/util.py"])

    assert delta["symbols"]["renamed"] == [
        {
            "old": _qn("pkg.util.helper"),
            "new": _qn("pkg.util.assist"),
            "path": "pkg/util.py",
        }
    ]
    assert delta["symbols"]["added"] == []
    assert delta["symbols"]["removed"] == []
    assert delta["dangling_callers"] == [
        {
            "caller": _qn("pkg.app.run"),
            "path": "pkg/app.py",
            "line": 5,
            "col": 11,
            "target": _qn("pkg.util.helper"),
            "renamed_to": _qn("pkg.util.assist"),
        }
    ]
    assert has_findings(delta)


def test_updated_caller_is_not_dangling(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def assist(a):"),
    )
    _write(root, "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper", "assist"))
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/app.py"])

    assert delta["dangling_callers"] == []
    assert delta["symbols"]["renamed"][0]["new"] == _qn("pkg.util.assist")


def test_two_arg_call_to_one_arg_function_is_an_arity_finding(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root, "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, 2)")
    )
    delta = _observe(root, store, updater, ["pkg/app.py"])

    assert delta["arity_findings"] == [
        {
            "caller": _qn("pkg.app.run"),
            "path": "pkg/app.py",
            "line": 5,
            "col": 11,
            "arg_count": 2,
            "kwarg_names": [],
            "declared_count": 1,
            "verdict": cs.DELTA_ARITY_TOO_MANY,
        }
    ]
    assert delta["signature_changes"] == []
    assert has_findings(delta)


def test_signature_change_lists_every_site_with_a_verdict(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def helper(a, b):"),
    )
    delta = _observe(root, store, updater, ["pkg/util.py"])

    assert delta["symbols"]["changed"] == [_qn("pkg.util.helper")]
    (change,) = delta["signature_changes"]
    assert change["before"] == ["a"]
    assert change["after"] == ["a", "b"]
    (site,) = change["sites"]
    assert site["path"] == "pkg/app.py"
    assert site["line"] == 5
    assert site["verdict"] == cs.DELTA_ARITY_POSSIBLY_MISSING
    assert site["declared_count"] == 2


def test_variadic_callee_is_never_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def helper(a, *rest):"),
    )
    _write(
        root,
        "pkg/app.py",
        FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, 2, 3)"),
    )
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/app.py"])

    assert delta["arity_findings"] == []
    assert not has_findings(delta)


def test_keyword_arguments_to_a_kwargs_callee_are_not_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def helper(a, **kw):"),
    )
    _write(
        root, "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, b=2)")
    )
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/app.py"])
    # One positional for one positional parameter; the keyword names nothing
    # in the positional list and is neutral, not an extra argument.
    assert delta["arity_findings"] == []
    assert not has_findings(delta)


def test_extra_positional_to_a_keyword_only_callee_is_too_many(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def helper(a, *, b=1):"),
    )
    _write(
        root,
        "pkg/app.py",
        FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, 2, b=3)"),
    )
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/app.py"])
    # A bare `*` accepts no extra positionals: this call raises TypeError.
    assert [f["verdict"] for f in delta["arity_findings"]] == [cs.DELTA_ARITY_TOO_MANY]


def test_pasting_a_helper_under_a_new_name_is_a_new_duplicate(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/extra.py", "def summarise(items):\n" + BIG_BODY)
    delta = _observe(root, store, updater, ["pkg/extra.py"])

    assert delta["symbols"]["added"] == [_qn("pkg.extra.summarise")]
    (duplicate,) = delta["new_duplicates"]
    assert duplicate["qualified_name"] == _qn("pkg.extra.summarise")
    assert duplicate["kind"] == cs.KIND_EXACT
    assert duplicate["similarity"] == 1.0
    assert duplicate["original"] == {
        "qualified_name": _qn("pkg.util.tally"),
        "path": "pkg/util.py",
        "start_line": 5,
    }


def test_new_import_cycle_is_reported_once(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        "from pkg.app import run\n\n\n" + FIXTURE["pkg/util.py"],
    )
    delta = _observe(root, store, updater, ["pkg/util.py"])
    assert delta["new_import_cycles"] == [[_qn("pkg.app"), _qn("pkg.util")]]

    # The cycle already exists now: a further neutral edit must not
    # report it again.
    _write(root, "pkg/util.py", (root / "pkg/util.py").read_text() + "# note\n")
    again = _observe(root, store, updater, ["pkg/util.py"])
    assert again["new_import_cycles"] == []
    assert not has_findings(again)


def test_tests_reaching_the_changed_symbols(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("a + 1", "a + 2"))
    delta = _observe(root, store, updater, ["pkg/util.py"])

    # A literal change moves neither the skeleton nor the signature, so
    # `changed` is empty; the edited file's symbols still decide the tests.
    assert delta["symbols"]["changed"] == []
    assert delta["tests_reaching"] == [
        {
            "qualified_name": _qn("tests.test_app.test_run"),
            "path": "tests/test_app.py",
            "depth": 2,
            "through": _qn("pkg.app.run"),
        }
    ]


def test_deleting_a_file_removes_its_symbols_and_dangles_callers(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    (root / "pkg/util.py").unlink()
    delta = _observe(root, store, updater, [], deleted=["pkg/util.py"])

    assert delta["removed_files"] == ["pkg/util.py"]
    assert _qn("pkg.util.helper") in delta["symbols"]["removed"]
    assert [d["caller"] for d in delta["dangling_callers"]] == [_qn("pkg.app.run")]
    assert delta["dangling_callers"][0]["renamed_to"] is None


def test_neutral_edit_reports_nothing(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/app.py", FIXTURE["pkg/app.py"] + "# trailing comment\n")
    delta = _observe(root, store, updater, ["pkg/app.py"])

    assert delta["symbols"] == {
        "added": [],
        "removed": [],
        "renamed": [],
        "changed": [],
    }
    assert not has_findings(delta)
    assert delta["reparsed"] == ["pkg/app.py"]
    assert delta["paths"] == ["pkg/app.py"]


def test_delta_overhead_is_small(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"].replace("a + 1", "a + 3"))
    delta = _observe(root, store, updater, ["pkg/util.py"])
    assert delta["reingest_ms"] > 0
    assert delta["delta_ms"] < 200


# --- units --------------------------------------------------------------------


def test_strongly_connected_components() -> None:
    graph = {
        "a": frozenset({"b"}),
        "b": frozenset({"c"}),
        "c": frozenset({"a"}),
        "d": frozenset({"a"}),
        "e": frozenset({"e"}),
    }
    components = {tuple(sorted(c)) for c in strongly_connected(graph)}
    assert components == {("a", "b", "c"), ("d",), ("e",)}


def test_normalise_paths_makes_repo_relative_posix(tmp_path: Path) -> None:
    inside = tmp_path / "pkg" / "x.py"
    outside = tmp_path.parent / "elsewhere.py"
    assert normalise_paths([inside, "pkg/y.py", outside], tmp_path) == [
        "pkg/x.py",
        "pkg/y.py",
    ]


# --- MCP write tools and cgr check ------------------------------------------


@pytest.fixture
def registry(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> tuple[Path, _StatefulIngestor, object]:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    root, store, updater = indexed
    ingestor = MagicMock()
    ingestor.fetch_all = store.fetch_all
    ingestor.list_projects.return_value = [PROJECT]
    registry = MCPToolsRegistry(
        project_root=str(root), ingestor=ingestor, cypher_gen=MagicMock()
    )
    registry._live_updater = updater
    return root, store, registry


async def test_write_file_appends_the_structural_delta(
    registry: tuple[Path, _StatefulIngestor, object],
) -> None:
    import json

    root, _store, reg = registry
    result = await reg.write_file(  # type: ignore[attr-defined]
        "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, 2)")
    )
    head, _sep, payload = result.partition(cs.MCP_DELTA_HEADER + "\n")
    assert head.startswith(cs.MCP_WRITE_SUCCESS.format(path="pkg/app.py"))
    delta = json.loads(payload)
    assert delta["arity_findings"][0]["verdict"] == cs.DELTA_ARITY_TOO_MANY
    assert delta["reparsed"] == ["pkg/app.py"]


async def test_surgical_replace_appends_the_structural_delta(
    registry: tuple[Path, _StatefulIngestor, object],
) -> None:
    import json

    _root, _store, reg = registry
    result = await reg.surgical_replace_code(  # type: ignore[attr-defined]
        "pkg/util.py", "def helper(a):", "def assist(a):"
    )
    _head, _sep, payload = result.partition(cs.MCP_DELTA_HEADER + "\n")
    delta = json.loads(payload)
    assert delta["dangling_callers"][0]["caller"] == _qn("pkg.app.run")


async def test_write_on_an_unindexed_project_appends_nothing(temp_repo: Path) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    ingestor = MagicMock()
    ingestor.list_projects.return_value = []
    reg = MCPToolsRegistry(
        project_root=str(temp_repo), ingestor=ingestor, cypher_gen=MagicMock()
    )
    result = await reg.write_file("fresh.py", "x = 1\n")
    assert result == cs.MCP_WRITE_SUCCESS.format(path="fresh.py")
    ingestor.fetch_all.assert_not_called()


@pytest.mark.parametrize(
    ("error", "invalidated"),
    [(RuntimeError("memgraph went away"), True), (ValueError("bad path"), False)],
)
def test_a_failed_write_reingest_invalidates_the_live_graph(
    temp_repo: Path, error: Exception, invalidated: bool
) -> None:
    from unittest.mock import MagicMock

    from codebase_rag.mcp.tools import MCPToolsRegistry

    registry = MCPToolsRegistry(
        project_root=str(temp_repo), ingestor=MagicMock(), cypher_gen=MagicMock()
    )
    updater = MagicMock()
    updater.project_name = "proj"
    updater.reingest.side_effect = error
    registry._live_updater = updater
    registry._graph_incomplete = False
    note = registry._delta_after_write(["pkg/app.py"])
    assert "memgraph" in note or "bad path" in note
    # A failure after the re-ingest began may have left a partial graph, so
    # the updater is dropped and the graph marked incomplete; a validation
    # error raised before any change leaves both as they were.
    assert (registry._live_updater is None) is invalidated
    assert registry._graph_incomplete is invalidated


def test_check_reports_the_delta_since_a_git_ref(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    import subprocess

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.structural_check import changed_since, run_check

    root, store, _updater = indexed
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=root,
        check=True,
    )
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def assist(a):"),
    )
    (root / "main.py").unlink()
    _write(root, "pkg/new.py", "def fresh():\n    return 1\n")

    assert changed_since(root, "HEAD") == (["pkg/new.py", "pkg/util.py"], ["main.py"])
    parsers, queries = load_parsers()
    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)
    assert delta["dangling_callers"][0]["target"] == _qn("pkg.util.helper")
    assert _qn("main.main") in delta["symbols"]["removed"]
    assert delta["symbols"]["added"] == [_qn("pkg.new.fresh")]


def test_check_rejects_a_dash_prefixed_base(temp_repo: Path) -> None:
    from codebase_rag.structural_check import CheckError, changed_since

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b"],
        cwd=root,
        check=True,
    )
    _write(root, "pkg/util.py", FIXTURE["pkg/util.py"] + "\n")
    # `--cached` would be read by git as its own option and compare the
    # index, hiding the working-tree edit.
    with pytest.raises(CheckError):
        changed_since(root, "--cached")
    assert changed_since(root, "HEAD") == (["pkg/util.py"], [])


def test_check_reads_paths_git_would_c_quote(temp_repo: Path) -> None:
    import subprocess

    from codebase_rag.structural_check import changed_since

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b"],
        cwd=root,
        check=True,
    )
    # Git C-quotes non-ASCII bytes in its default output; `-z` keeps them
    # raw. A space would NOT do here: git leaves a space bare in
    # `ls-files --others`, so the fixture has to carry a byte git actually
    # quotes or the test stops detecting the quoting bug. A tab is quoted
    # but is not a legal NTFS filename character, and the fixture write
    # itself raised OSError on the Windows runners.
    _write(root, "pkg/odd\u00e9name.py", "def odd():\n    return 1\n")
    assert changed_since(root, "HEAD") == (["pkg/odd\u00e9name.py"], [])


def test_check_uses_the_scope_the_graph_was_indexed_under(temp_repo: Path) -> None:
    from codebase_rag.structural_check import indexed_scope

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    # No stamp yet: only `.cgrignore` (here, nothing) defines the scope.
    assert indexed_scope(root, PROJECT) == (None, None)
    store = _StatefulIngestor()
    parsers, queries = __import__(
        "codebase_rag.parser_loader", fromlist=["load_parsers"]
    ).load_parsers()
    GraphUpdater(
        ingestor=store,
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=PROJECT,
        exclude_paths=frozenset({"generated_src"}),
        unignore_paths=frozenset({"build"}),
    ).run(force=True)
    # The completed run stamped its CLI-only scope; the check reads it back.
    assert indexed_scope(root, PROJECT) == (
        frozenset({"generated_src"}),
        frozenset({"build"}),
    )
    # Another project indexed from the same tree overwrites the stamp; the
    # check for the first project refuses rather than borrowing that scope.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="other_project",
    ).run(force=True)
    from codebase_rag.structural_check import CheckError

    with pytest.raises(CheckError):
        indexed_scope(root, PROJECT)
    assert indexed_scope(root, "other_project") == (None, None)


def test_check_keeps_excluded_files_out_of_the_graph(
    temp_repo: Path,
) -> None:
    import subprocess

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.structural_check import run_check

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b"],
        cwd=root,
        check=True,
    )
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    _write(root, "generated_src/thing.py", "def vendored():\n    return 1\n")
    parsers, queries = load_parsers()
    delta = run_check(
        root,
        "HEAD",
        PROJECT,
        store,
        parsers,
        queries,
        exclude_paths=frozenset({"generated_src"}),
    )
    # The excluded file differs from the base but never enters the graph.
    assert _qn("generated_src.thing.vendored") not in delta["symbols"]["added"]
    assert all("generated_src" not in p for p in delta["reparsed"])


def test_check_works_when_the_project_is_below_the_git_toplevel(
    temp_repo: Path,
) -> None:
    import subprocess

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.structural_check import changed_since, run_check

    top = temp_repo / "mono"
    root = top / "svc"
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    subprocess.run(["git", "init", "-q"], cwd=top, check=True)
    subprocess.run(["git", "add", "-A"], cwd=top, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b"],
        cwd=top,
        check=True,
    )
    store = _StatefulIngestor()
    _updater(store, root).run(force=True)
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def assist(a):"),
    )
    # `git diff` names files relative to the toplevel unless told otherwise;
    # the project root is one level below it.
    assert changed_since(root, "HEAD")[0] == ["pkg/util.py"]
    parsers, queries = load_parsers()
    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)
    (rename,) = delta["symbols"]["renamed"]
    assert rename["old"] == _qn("pkg.util.helper")
    assert rename["new"] == _qn("pkg.util.assist")
    assert delta["dangling_callers"][0]["target"] == _qn("pkg.util.helper")


def test_check_ignores_cgr_state_files(
    temp_repo: Path,
) -> None:
    import subprocess

    from codebase_rag.parser_loader import load_parsers
    from codebase_rag.structural_check import changed_since, run_check

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b"],
        cwd=root,
        check=True,
    )
    store = _StatefulIngestor()
    # Indexing after the commit leaves cgr's state files untracked.
    _updater(store, root).run(force=True)
    assert any(p.name.startswith(".cgr-") for p in root.iterdir())
    assert changed_since(root, "HEAD") == ([], [])
    parsers, queries = load_parsers()
    delta = run_check(root, "HEAD", PROJECT, store, parsers, queries)
    assert delta["paths"] == []
    assert delta["reparsed"] == []


def test_keyword_arguments_are_counted_once(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(
        root,
        "pkg/util.py",
        FIXTURE["pkg/util.py"].replace("def helper(a):", "def helper(a, b=1):"),
    )
    _write(
        root, "pkg/app.py", FIXTURE["pkg/app.py"].replace("helper(1)", "helper(1, b=2)")
    )
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/app.py"])
    # `arg_count` already includes the keyword: two arguments for two
    # parameters is exact, not one too many.
    assert delta["arity_findings"] == []
    (change,) = delta["signature_changes"]
    assert change["sites"][0]["verdict"] == cs.DELTA_ARITY_OK


def test_moved_definition_is_paired_across_files(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    # `tally` leaves util.py for core.py unchanged: a move, reported as a
    # rename across files rather than a removal plus an addition.
    _write(root, "pkg/util.py", "def helper(a):\n    return a + 1\n")
    _write(root, "pkg/core.py", "def tally(items):\n" + BIG_BODY)
    delta = _observe(root, store, updater, ["pkg/util.py", "pkg/core.py"])
    assert delta["symbols"]["renamed"] == [
        {
            "old": _qn("pkg.util.tally"),
            "new": _qn("pkg.core.tally"),
            "path": "pkg/util.py",
        }
    ]
    assert delta["symbols"]["added"] == []
    assert delta["symbols"]["removed"] == []
    assert delta["new_duplicates"] == []


def test_check_accepts_a_stamp_written_under_the_repositorys_default_name(
    temp_repo: Path,
) -> None:
    from codebase_rag.structural_check import CheckError, indexed_scope
    from codebase_rag.utils.path_utils import derive_project_name

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = __import__(
        "codebase_rag.parser_loader", fromlist=["load_parsers"]
    ).load_parsers()
    # `cgr index` without --project: GraphUpdater stamps the bare directory
    # name, while `cgr check` derives the digest-suffixed one for the same tree.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        exclude_paths=frozenset({"generated_src"}),
    ).run(force=True)
    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"generated_src"}),
        None,
    )
    # A stamp from a project that is not this tree under any name still refuses.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name="other_project",
    ).run(force=True)
    own_name = derive_project_name(root)
    with pytest.raises(CheckError):
        indexed_scope(root, own_name)


def test_check_refuses_the_default_names_stamp_for_a_named_project(
    temp_repo: Path,
) -> None:
    """An explicit --project owns only its own name.

    The unnamed-run names (derived and bare directory) are what this tree
    would have been called had the run not named it, so lending them to a
    DIFFERENT explicitly requested project applies one project's exclusions
    to another's graph.
    """
    from codebase_rag.structural_check import CheckError, indexed_scope
    from codebase_rag.utils.path_utils import derive_project_name

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = __import__(
        "codebase_rag.parser_loader", fromlist=["load_parsers"]
    ).load_parsers()
    # An unnamed run: stamps the bare directory name, excluding `generated_src`.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        exclude_paths=frozenset({"generated_src"}),
    ).run(force=True)
    # `cgr check --project other_project` must NOT inherit that scope.
    with pytest.raises(CheckError):
        indexed_scope(root, "other_project")
    # The same stamp still serves the unnamed run it belongs to, under
    # either spelling of this tree's identity: `list_projects` shows the
    # derived name, so asking by that name must not be refused.
    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"generated_src"}),
        None,
    )
    assert indexed_scope(root, root.name) == (
        frozenset({"generated_src"}),
        None,
    )


def test_a_callee_lookup_does_not_reach_another_project() -> None:
    """The by-qn definition lookup is scoped to the project asking.

    `CYPHER_DELTA_SITES` prefix-scopes only the CALLER side, so a callee
    name that no definition in the touched files resolves reaches the by-qn
    lookup. In a shared graph holding several projects an identically named
    definition from another project would otherwise answer it and supply
    the parameter list the arity verdict is computed from.
    """
    from codebase_rag.structural_delta import snapshot

    store = _StatefulIngestor()
    store.ensure_node_batch(
        cs.NodeLabel.FUNCTION.value,
        {
            cs.KEY_QUALIFIED_NAME: "proj.pkg.app.main",
            cs.KEY_NAME: "main",
            cs.KEY_PATH: "pkg/app.py",
            cs.KEY_START_LINE: 1,
        },
    )
    # Another project's symbol, outside the touched paths, so the only route
    # to it is the by-qn lookup.
    store.ensure_node_batch(
        cs.NodeLabel.FUNCTION.value,
        {
            cs.KEY_QUALIFIED_NAME: "other.pkg.util.helper",
            cs.KEY_NAME: "helper",
            cs.KEY_PATH: "elsewhere/util.py",
            cs.KEY_START_LINE: 1,
            cs.KEY_POSITIONAL_PARAMS: ["a"],
        },
    )
    store.ensure_relationship_batch(
        (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "proj.pkg.app.main"),
        cs.RelationshipType.CALLS.value,
        (cs.NodeLabel.FUNCTION.value, cs.KEY_QUALIFIED_NAME, "other.pkg.util.helper"),
        {cs.KEY_LINE: 5, cs.KEY_COL: 1, cs.KEY_ARG_COUNT: 3},
    )

    taken = snapshot(store.fetch_all, "proj", ["pkg/app.py"])

    # The call site is seen -- the lookup really is reached, so the empty
    # `callees` below is a refusal rather than a walk that never happened.
    assert [site.callee for site in taken.sites] == ["other.pkg.util.helper"]
    assert taken.callees == {}


def test_check_refuses_a_named_projects_stamp_for_the_default_project(
    temp_repo: Path,
) -> None:
    """A project named after its own directory is not the default project.

    `cgr index --project myrepo` in a tree called `myrepo` writes a stamp
    whose owner string is identical to the one an unnamed run would write,
    so the name alone cannot say which it was. The stamp records it, and
    the default (digest-derived) project must not inherit that scope.
    """
    from codebase_rag.structural_check import CheckError, indexed_scope
    from codebase_rag.utils.path_utils import derive_project_name

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = __import__(
        "codebase_rag.parser_loader", fromlist=["load_parsers"]
    ).load_parsers()
    # Named explicitly, and the name happens to equal the directory name.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        project_name=root.name,
        exclude_paths=frozenset({"generated_src"}),
    ).run(force=True)
    # The default project is a different graph and must not take that scope.
    own_name = derive_project_name(root)
    with pytest.raises(CheckError):
        indexed_scope(root, own_name)
    # The project it belongs to still reads it.
    assert indexed_scope(root, root.name, explicit=True) == (
        frozenset({"generated_src"}),
        None,
    )


def test_check_refuses_an_unnamed_stamp_for_a_named_project(
    temp_repo: Path,
) -> None:
    """An unnamed run overwrites the stamp; the named project's scope is gone.

    The stamp is per repository, so a later `cgr index` with no --project
    replaces whatever `cgr index --project myrepo` had written. Its owner
    string is the directory name either way, so only the recorded
    provenance separates them -- and once overwritten the named project's
    scope is not recoverable, so the check must refuse rather than apply
    the unnamed run's.
    """
    from codebase_rag.structural_check import CheckError, indexed_scope
    from codebase_rag.utils.path_utils import derive_project_name

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    parsers, queries = __import__(
        "codebase_rag.parser_loader", fromlist=["load_parsers"]
    ).load_parsers()
    # The unnamed run, which owns the stamp on disk.
    GraphUpdater(
        ingestor=_StatefulIngestor(),
        repo_path=root,
        parsers=parsers,
        queries=queries,
        exclude_paths=frozenset({"generated_src"}),
    ).run(force=True)
    # `cgr check --project <basename>` is a different graph and must refuse.
    with pytest.raises(CheckError):
        indexed_scope(root, root.name, explicit=True)
    # The unnamed default it belongs to still reads it -- so the refusal
    # above is about provenance, not a stamp that cannot be read at all.
    assert indexed_scope(root, derive_project_name(root)) == (
        frozenset({"generated_src"}),
        None,
    )


@pytest.mark.parametrize("base", ["HEAD~1..HEAD", "HEAD~1...HEAD", "no-such-rev"])
def test_check_refuses_a_base_that_is_not_one_commit(
    temp_repo: Path, base: str
) -> None:
    from codebase_rag.structural_check import CheckError, changed_since

    root = temp_repo / PROJECT
    for rel, text in FIXTURE.items():
        _write(root, rel, text)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "one")
    _write(root, "pkg/extra.py", "X = 1\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "two")
    # The range would compare the two commits and miss this working-tree edit.
    _write(root, "pkg/extra.py", "X = 2\n")
    with pytest.raises(CheckError, match="one commit"):
        changed_since(root, base)
    assert changed_since(root, "HEAD") == (["pkg/extra.py"], [])


# An EMPTY container carries no fingerprint, so the only signal the pairing
# ever had was "one of this label left the file and one appeared" -- which an
# unrelated replacement satisfies exactly. The contract treats an unexpected
# rename as a failure, so an invented one rolls back a correct edit
# (Greptile, PR #1547).
def test_an_unrelated_empty_class_is_not_reported_as_a_rename(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/empty.py", "class Alpha:\n    pass\n")
    _observe(root, store, updater, ["pkg/empty.py"])

    # A DIFFERENT empty class, declared lower down: not a rename of Alpha.
    _write(root, "pkg/empty.py", "# a new file entirely\n\nclass Beta:\n    pass\n")
    delta = _observe(root, store, updater, ["pkg/empty.py"])

    assert delta["symbols"]["renamed"] == []
    assert _qn("pkg.empty.Alpha") in delta["symbols"]["removed"]
    assert _qn("pkg.empty.Beta") in delta["symbols"]["added"]


def test_an_empty_class_renamed_in_place_is_still_a_rename(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    # The control. Without it, refusing every empty-container pairing would
    # pass the test above and silently drop a real rename.
    root, store, updater = indexed
    _write(root, "pkg/empty.py", "class Alpha:\n    pass\n")
    _observe(root, store, updater, ["pkg/empty.py"])

    # Same declaration line, new name: the evidence a real rename leaves.
    _write(root, "pkg/empty.py", "class Renamed:\n    pass\n")
    delta = _observe(root, store, updater, ["pkg/empty.py"])

    assert delta["symbols"]["renamed"] == [
        {
            "old": _qn("pkg.empty.Alpha"),
            "new": _qn("pkg.empty.Renamed"),
            "path": "pkg/empty.py",
        }
    ]
    assert delta["symbols"]["added"] == []
    assert delta["symbols"]["removed"] == []
