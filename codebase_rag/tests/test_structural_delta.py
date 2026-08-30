# Structural delta after a write (issue #1525): the touched files' subgraph
# is read before and after the scoped re-ingest and the difference reported
# as dangling callers, signature changes with per-site arity verdicts, new
# duplicates, new import cycles and the tests reaching the changed symbols.
# The graph is the in-memory stateful ingestor the reingest tests use, which
# answers the delta's fixed queries from its node and edge tables.

from __future__ import annotations

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
    assert delta["symbols"]["added"] == [] and delta["symbols"]["removed"] == []
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
    assert change["before"] == ["a"] and change["after"] == ["a", "b"]
    (site,) = change["sites"]
    assert site["path"] == "pkg/app.py" and site["line"] == 5
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


def test_pasting_a_helper_under_a_new_name_is_a_new_duplicate(
    indexed: tuple[Path, _StatefulIngestor, GraphUpdater],
) -> None:
    root, store, updater = indexed
    _write(root, "pkg/extra.py", "def summarise(items):\n" + BIG_BODY)
    delta = _observe(root, store, updater, ["pkg/extra.py"])

    assert delta["symbols"]["added"] == [_qn("pkg.extra.summarise")]
    (duplicate,) = delta["new_duplicates"]
    assert duplicate["qualified_name"] == _qn("pkg.extra.summarise")
    assert duplicate["kind"] == cs.KIND_EXACT and duplicate["similarity"] == 1.0
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
    assert delta["symbols"]["added"] == [] and delta["symbols"]["removed"] == []
    assert delta["new_duplicates"] == []
