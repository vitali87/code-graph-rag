"""The reference graphs are written so a difference means a change (issue #3629).

`fixtures/reference_graphs` holds the graphs of pinned public repositories,
one or more per language, as `cgr` builds and exports them, written by
`scripts/reference_graphs.py` and checked by the `Reference Graphs` CI job.
These tests cover what the job cannot: that the written form does not depend
on where or when the index ran, and that every listed repository has a graph.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from codebase_rag import constants as cs
from scripts import reference_graphs
from scripts.reference_graphs import (
    REFERENCE_DIR,
    ExportRecord,
    _command,
    _index_env,
    canonical,
    difference,
    graph_path,
    load_manifest,
    read_graph,
    write_graph,
)

ROOT = Path("/work/checkout/demo")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _export(order: list[int]) -> ExportRecord:
    """A small export whose node ids follow `order`: the database's row
    numbers, which differ from one run to the next."""
    project, folder, function, method = order
    return {
        "nodes": [
            {
                "node_id": function,
                "labels": ["Function"],
                "properties": {
                    "qualified_name": "demo.app.main",
                    "name": "main",
                    "path": "app.py",
                    "start_line": 1,
                },
            },
            {
                "node_id": project,
                "labels": ["Project"],
                "properties": {
                    "name": "demo",
                    "root_path": str(ROOT),
                    "last_synced_at": "2026-11-18T13:14:10+00:00",
                },
            },
            {
                "node_id": folder,
                "labels": ["Folder"],
                "properties": {
                    "absolute_path": f"{ROOT}/pkg",
                    "path": "pkg",
                    "name": "pkg",
                },
            },
            {
                "node_id": method,
                "labels": ["Method"],
                "properties": {"qualified_name": "demo.app.C.run", "name": "run"},
            },
        ],
        "relationships": [
            {
                "from_id": project,
                "to_id": folder,
                "type": "CONTAINS_FOLDER",
                "properties": {},
            },
            {
                "from_id": function,
                "to_id": method,
                "type": "CALLS",
                "properties": {"line": 2, "resolution": "exact"},
            },
        ],
    }


def test_a_graph_reads_the_same_whatever_row_numbers_the_database_gave() -> None:
    assert canonical(_export([1, 2, 3, 4]), ROOT) == canonical(
        _export([40, 7, 12, 3]), ROOT
    )


def test_a_graph_names_no_directory_of_the_machine_and_no_time() -> None:
    text = "\n".join(canonical(_export([1, 2, 3, 4]), ROOT))

    assert str(ROOT) not in text
    assert f"{cs.REFERENCE_ROOT_PLACEHOLDER}/pkg" in text
    assert "last_synced_at" not in text


def test_a_node_is_named_by_the_property_its_label_is_keyed_on() -> None:
    lines = canonical(_export([1, 2, 3, 4]), ROOT)
    calls = next(line for line in lines if "\tCALLS\t" in line)

    assert calls.split(cs.REFERENCE_FIELD_SEPARATOR)[2:6] == [
        "Function",
        '"demo.app.main"',
        "Method",
        '"demo.app.C.run"',
    ]
    assert lines == sorted(lines)


def test_an_edge_recorded_twice_is_a_difference() -> None:
    once = canonical(_export([1, 2, 3, 4]), ROOT)
    twice_export = _export([1, 2, 3, 4])
    relationships = twice_export["relationships"]
    assert isinstance(relationships, list)
    relationships.append(relationships[-1])

    diff = difference(once, canonical(twice_export, ROOT))

    assert len(diff) == 1
    assert diff[0].startswith("+ E\tCALLS")


def test_difference_marks_each_side() -> None:
    assert difference(["a", "b"], ["b", "c"]) == ["- a", "+ c"]
    assert difference(["a"], ["a"]) == []


def test_an_index_runs_with_the_frontends_that_need_no_compiler(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GO_FRONTEND", "gotypes")
    monkeypatch.setitem(
        reference_graphs._CALLER_ENV, "SQLITE_GRAPH_PATH", "/somewhere/else.sqlite"
    )

    env = _index_env(tmp_path, seed=3)

    assert env["GO_FRONTEND"] == cs.REFERENCE_ENV["GO_FRONTEND"]
    assert "SQLITE_GRAPH_PATH" not in env
    assert env[cs.REFERENCE_HOME_ENV] == str(tmp_path)
    assert env[cs.REFERENCE_SEED_ENV] == "3"


def test_every_listed_repository_is_pinned_and_has_a_recorded_graph() -> None:
    repos = load_manifest()
    names = [repo["name"] for repo in repos]
    languages = {str(language) for language in cs.SupportedLanguage}

    assert len(names) == len(set(names))
    for repo in repos:
        assert _COMMIT.fullmatch(repo["commit"]), repo["name"]
        assert repo["language"] in languages, repo["name"]
        assert graph_path(repo).is_file(), repo["name"]


def test_every_language_has_a_reference_repository() -> None:
    # Checked against the languages cgr supports, not the manifest, so
    # dropping a language's entry and its graph together still fails.
    covered = {repo["language"] for repo in load_manifest()}
    required = {
        str(language) for language in cs.SupportedLanguage
    } - cs.REFERENCE_EXEMPT_LANGUAGES

    assert required <= covered, sorted(required - covered)


def test_a_record_holding_a_unicode_line_separator_reads_back_whole(
    tmp_path: Path,
) -> None:
    lines = ['N\tFunction\t"demo.f"\t{"docstring":"one\u2028two"}', "N\tx"]
    path = tmp_path / "demo.graph"

    write_graph(path, lines)

    assert read_graph(path) == lines


def test_the_callers_settings_reach_the_index_in_no_spelling(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(
        reference_graphs._CALLER_ENV, "sqlite_graph_path", "/somewhere/else.sqlite"
    )
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "go_frontend", "gotypes")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "cgr_home", "/somewhere/home")

    env = _index_env(tmp_path, seed=None)

    assert {name.upper() for name in env if name.islower()} & {
        "SQLITE_GRAPH_PATH",
        "GO_FRONTEND",
        "CGR_HOME",
    } == set()
    assert env[cs.REFERENCE_HOME_ENV] == str(tmp_path)


def test_no_cgr_setting_of_the_caller_reaches_the_index(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "CGR_CAPTURE", "-CALLS")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "PYTHON_FRONTEND", "jedi")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "PATH_FOR_THE_INDEX", "kept")

    env = _index_env(tmp_path, seed=None)

    assert "CGR_CAPTURE" not in env
    assert env["PYTHON_FRONTEND"] == cs.REFERENCE_ENV["PYTHON_FRONTEND"]
    assert env["PATH_FOR_THE_INDEX"] == "kept"


def test_a_forward_slash_spelling_of_the_root_is_replaced_too() -> None:
    root = Path("/work/checkout/demo")
    export = _export([1, 2, 3, 4])
    nodes = export["nodes"]
    assert isinstance(nodes, list)
    folder = nodes[2]
    assert isinstance(folder, dict)
    properties = folder["properties"]
    assert isinstance(properties, dict)
    properties["absolute_path"] = f"{root.as_posix()}/pkg"

    text = "\n".join(canonical(export, root))

    assert root.as_posix() not in text


def test_an_export_without_its_lists_is_refused() -> None:
    with pytest.raises(SystemExit):
        canonical({"nodes": []}, ROOT)
    assert canonical({"nodes": [], "relationships": []}, ROOT) == []


def test_a_recording_whose_runs_disagree_writes_nothing(
    monkeypatch, tmp_path: Path
) -> None:
    repo = load_manifest()[0]
    graph = graph_path(repo, tmp_path)
    graph.write_text("kept\n", encoding="utf-8")
    monkeypatch.setattr(reference_graphs, "fetch", lambda repo, cache: tmp_path)
    monkeypatch.setattr(
        reference_graphs,
        "_graphs",
        lambda cgr, repo, checkout, runs: (["a"], [(1, ["b"])]),
    )

    status = reference_graphs.run("record", [repo], ["cgr"], tmp_path, 2, tmp_path)

    assert status == cs.REFERENCE_EXIT_DIFFERENT
    assert graph.read_text(encoding="utf-8") == "kept\n"


def test_the_git_calls_ignore_a_repository_location_in_the_environment(
    monkeypatch,
) -> None:
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GIT_DIR", "/elsewhere/.git")
    monkeypatch.setitem(reference_graphs._CALLER_ENV, "GIT_CONFIG_GLOBAL", "/g")

    env = reference_graphs._git_env()

    assert "GIT_DIR" not in env
    assert env["GIT_CONFIG_GLOBAL"] == "/g"


def test_a_relative_cgr_program_is_made_absolute() -> None:
    program, *rest = _command("./target/release/cgr --flag")

    assert Path(program).is_absolute()
    assert rest == ["--flag"]
    assert _command("cgr") == ["cgr"]


def test_no_recorded_graph_names_a_directory_outside_its_checkout() -> None:
    # A path the placeholder missed would differ on every machine.
    for repo in load_manifest():
        for line in read_graph(graph_path(repo)):
            assert '"absolute_path":"/' not in line, (repo["name"], line[:200])
            assert '"root_path":"/' not in line, (repo["name"], line[:200])


def test_every_recorded_graph_is_listed() -> None:
    listed = {graph_path(repo).name for repo in load_manifest()}
    recorded = {
        path.name for path in REFERENCE_DIR.glob(f"*{cs.REFERENCE_GRAPH_SUFFIX}")
    }

    assert recorded == listed
